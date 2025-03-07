from __future__ import annotations

import base64
import logging
import os
from io import BytesIO
from typing import (
    Any,
    AsyncIterator,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
)
from urllib.parse import urlparse

import google.api_core

# TODO: remove ignore once the google package is published with types
import google.generativeai as genai  # type: ignore[import]
import requests
from langchain_core.callbacks.manager import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    ChatMessage,
    ChatMessageChunk,
    HumanMessage,
    HumanMessageChunk,
    SystemMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.pydantic_v1 import Field, SecretStr, root_validator
from langchain_core.utils import get_from_dict_or_env
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from langchain_google_genai._common import GoogleGenerativeAIError

IMAGE_TYPES: Tuple = ()
try:
    import PIL
    from PIL.Image import Image

    IMAGE_TYPES = IMAGE_TYPES + (Image,)
except ImportError:
    PIL = None  # type: ignore
    Image = None  # type: ignore

logger = logging.getLogger(__name__)


class ChatGoogleGenerativeAIError(GoogleGenerativeAIError):
    """
    Custom exception class for errors associated with the `Google GenAI` API.

    This exception is raised when there are specific issues related to the
    Google genai API usage in the ChatGoogleGenerativeAI class, such as unsupported
    message types or roles.
    """


def _create_retry_decorator() -> Callable[[Any], Any]:
    """
    Creates and returns a preconfigured tenacity retry decorator.

    The retry decorator is configured to handle specific Google API exceptions
    such as ResourceExhausted and ServiceUnavailable. It uses an exponential
    backoff strategy for retries.

    Returns:
        Callable[[Any], Any]: A retry decorator configured for handling specific
        Google API exceptions.
    """
    multiplier = 2
    min_seconds = 1
    max_seconds = 60
    max_retries = 10

    return retry(
        reraise=True,
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=multiplier, min=min_seconds, max=max_seconds),
        retry=(
            retry_if_exception_type(google.api_core.exceptions.ResourceExhausted)
            | retry_if_exception_type(google.api_core.exceptions.ServiceUnavailable)
            | retry_if_exception_type(google.api_core.exceptions.GoogleAPIError)
        ),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )


def _chat_with_retry(generation_method: Callable, **kwargs: Any) -> Any:
    """
    Executes a chat generation method with retry logic using tenacity.

    This function is a wrapper that applies a retry mechanism to a provided
    chat generation function. It is useful for handling intermittent issues
    like network errors or temporary service unavailability.

    Args:
        generation_method (Callable): The chat generation method to be executed.
        **kwargs (Any): Additional keyword arguments to pass to the generation method.

    Returns:
        Any: The result from the chat generation method.
    """
    retry_decorator = _create_retry_decorator()

    @retry_decorator
    def _chat_with_retry(**kwargs: Any) -> Any:
        try:
            return generation_method(**kwargs)
        # Do not retry for these errors.
        except google.api_core.exceptions.FailedPrecondition as exc:
            if "location is not supported" in exc.message:
                error_msg = (
                    "Your location is not supported by google-generativeai "
                    "at the moment. Try to use ChatVertexAI LLM from "
                    "langchain_google_vertexai."
                )
                raise ValueError(error_msg)

        except google.api_core.exceptions.InvalidArgument as e:
            raise ChatGoogleGenerativeAIError(
                f"Invalid argument provided to Gemini: {e}"
            ) from e
        except Exception as e:
            raise e

    return _chat_with_retry(**kwargs)


async def _achat_with_retry(generation_method: Callable, **kwargs: Any) -> Any:
    """
    Executes a chat generation method with retry logic using tenacity.

    This function is a wrapper that applies a retry mechanism to a provided
    chat generation function. It is useful for handling intermittent issues
    like network errors or temporary service unavailability.

    Args:
        generation_method (Callable): The chat generation method to be executed.
        **kwargs (Any): Additional keyword arguments to pass to the generation method.

    Returns:
        Any: The result from the chat generation method.
    """
    retry_decorator = _create_retry_decorator()
    from google.api_core.exceptions import InvalidArgument  # type: ignore

    @retry_decorator
    async def _achat_with_retry(**kwargs: Any) -> Any:
        try:
            return await generation_method(**kwargs)
        except InvalidArgument as e:
            # Do not retry for these errors.
            raise ChatGoogleGenerativeAIError(
                f"Invalid argument provided to Gemini: {e}"
            ) from e
        except Exception as e:
            raise e

    return await _achat_with_retry(**kwargs)


def _is_openai_parts_format(part: dict) -> bool:
    return "type" in part


def _is_vision_model(model: str) -> bool:
    return "vision" in model


def _is_url(s: str) -> bool:
    try:
        result = urlparse(s)
        return all([result.scheme, result.netloc])
    except Exception as e:
        logger.debug(f"Unable to parse URL: {e}")
        return False


def _is_b64(s: str) -> bool:
    return s.startswith("data:image")


def _load_image_from_gcs(path: str, project: Optional[str] = None) -> Image:
    try:
        from google.cloud import storage  # type: ignore[attr-defined]
    except ImportError:
        raise ImportError(
            "google-cloud-storage is required to load images from GCS."
            " Install it with `pip install google-cloud-storage`"
        )
    if PIL is None:
        raise ImportError(
            "PIL is required to load images. Please install it "
            "with `pip install pillow`"
        )

    gcs_client = storage.Client(project=project)
    pieces = path.split("/")
    blobs = list(gcs_client.list_blobs(pieces[2], prefix="/".join(pieces[3:])))
    if len(blobs) > 1:
        raise ValueError(f"Found more than one candidate for {path}!")
    img_bytes = blobs[0].download_as_bytes()
    return PIL.Image.open(BytesIO(img_bytes))


def _url_to_pil(image_source: str) -> Image:
    if PIL is None:
        raise ImportError(
            "PIL is required to load images. Please install it "
            "with `pip install pillow`"
        )
    try:
        if isinstance(image_source, IMAGE_TYPES):
            return image_source  # type: ignore[return-value]
        elif _is_url(image_source):
            if image_source.startswith("gs://"):
                return _load_image_from_gcs(image_source)
            response = requests.get(image_source)
            response.raise_for_status()
            return PIL.Image.open(BytesIO(response.content))
        elif _is_b64(image_source):
            _, encoded = image_source.split(",", 1)
            data = base64.b64decode(encoded)
            return PIL.Image.open(BytesIO(data))
        elif os.path.exists(image_source):
            return PIL.Image.open(image_source)
        else:
            raise ValueError(
                "The provided string is not a valid URL, base64, or file path."
            )
    except Exception as e:
        raise ValueError(f"Unable to process the provided image source: {e}")


def _convert_to_parts(
    raw_content: Union[str, Sequence[Union[str, dict]]],
) -> List[genai.types.PartType]:
    """Converts a list of LangChain messages into a google parts."""
    parts = []
    content = [raw_content] if isinstance(raw_content, str) else raw_content
    for part in content:
        if isinstance(part, str):
            parts.append(genai.types.PartDict(text=part))
        elif isinstance(part, Mapping):
            # OpenAI Format
            if _is_openai_parts_format(part):
                if part["type"] == "text":
                    parts.append({"text": part["text"]})
                elif part["type"] == "image_url":
                    img_url = part["image_url"]
                    if isinstance(img_url, dict):
                        if "url" not in img_url:
                            raise ValueError(
                                f"Unrecognized message image format: {img_url}"
                            )
                        img_url = img_url["url"]
                    parts.append({"inline_data": _url_to_pil(img_url)})
                else:
                    raise ValueError(f"Unrecognized message part type: {part['type']}")
            else:
                # Yolo
                logger.warning(
                    "Unrecognized message part format. Assuming it's a text part."
                )
                parts.append(part)
        else:
            # TODO: Maybe some of Google's native stuff
            # would hit this branch.
            raise ChatGoogleGenerativeAIError(
                "Gemini only supports text and inline_data parts."
            )
    return parts


def _parse_chat_history(
    input_messages: Sequence[BaseMessage], convert_system_message_to_human: bool = False
) -> List[genai.types.ContentDict]:
    messages: List[genai.types.MessageDict] = []

    raw_system_message: Optional[SystemMessage] = None
    for i, message in enumerate(input_messages):
        if (
            i == 0
            and isinstance(message, SystemMessage)
            and not convert_system_message_to_human
        ):
            raise ValueError(
                """SystemMessages are not yet supported!

To automatically convert the leading SystemMessage to a HumanMessage,
set  `convert_system_message_to_human` to True. Example:

llm = ChatGoogleGenerativeAI(model="gemini-pro", convert_system_message_to_human=True)
"""
            )
        elif i == 0 and isinstance(message, SystemMessage):
            raw_system_message = message
            continue
        elif isinstance(message, AIMessage):
            role = "model"
        elif isinstance(message, HumanMessage):
            role = "user"
        else:
            raise ValueError(
                f"Unexpected message with type {type(message)} at the position {i}."
            )

        parts = _convert_to_parts(message.content)
        if raw_system_message:
            if role == "model":
                raise ValueError(
                    "SystemMessage should be followed by a HumanMessage and "
                    "not by AIMessage."
                )
            parts = _convert_to_parts(raw_system_message.content) + parts
            raw_system_message = None
        messages.append({"role": role, "parts": parts})
    return messages


def _parts_to_content(parts: List[genai.types.PartType]) -> Union[List[dict], str]:
    """Converts a list of Gemini API Part objects into a list of LangChain messages."""
    if len(parts) == 1 and parts[0].text is not None and not parts[0].inline_data:
        # Simple text response. The typical response
        return parts[0].text
    elif not parts:
        logger.warning("Gemini produced an empty response.")
        return ""
    messages = []
    for part in parts:
        if part.text is not None:
            messages.append(
                {
                    "type": "text",
                    "text": part.text,
                }
            )
        else:
            # TODO: Handle inline_data if that's a thing?
            raise ChatGoogleGenerativeAIError(f"Unexpected part type. {part}")
    return messages


def _response_to_result(
    response: genai.types.GenerateContentResponse,
    ai_msg_t: Type[BaseMessage] = AIMessage,
    human_msg_t: Type[BaseMessage] = HumanMessage,
    chat_msg_t: Type[BaseMessage] = ChatMessage,
    generation_t: Type[ChatGeneration] = ChatGeneration,
) -> ChatResult:
    """Converts a PaLM API response into a LangChain ChatResult."""
    llm_output = {}
    if response.prompt_feedback:
        try:
            prompt_feedback = type(response.prompt_feedback).to_dict(
                response.prompt_feedback, use_integers_for_enums=False
            )
            llm_output["prompt_feedback"] = prompt_feedback
        except Exception as e:
            logger.debug(f"Unable to convert prompt_feedback to dict: {e}")

    generations: List[ChatGeneration] = []

    role_map = {
        "model": ai_msg_t,
        "user": human_msg_t,
    }
    for candidate in response.candidates:
        content = candidate.content
        parts_content = _parts_to_content(content.parts)
        if content.role not in role_map:
            logger.warning(
                f"Unrecognized role: {content.role}. Treating as a ChatMessage."
            )
            msg = chat_msg_t(content=parts_content, role=content.role)
        else:
            msg = role_map[content.role](content=parts_content)
        generation_info = {}
        if candidate.finish_reason:
            generation_info["finish_reason"] = candidate.finish_reason.name
        if candidate.safety_ratings:
            generation_info["safety_ratings"] = [
                type(rating).to_dict(rating) for rating in candidate.safety_ratings
            ]
        generations.append(generation_t(message=msg, generation_info=generation_info))
    if not response.candidates:
        # Likely a "prompt feedback" violation (e.g., toxic input)
        # Raising an error would be different than how OpenAI handles it,
        # so we'll just log a warning and continue with an empty message.
        logger.warning(
            "Gemini produced an empty response. Continuing with empty message\n"
            f"Feedback: {response.prompt_feedback}"
        )
        generations = [generation_t(message=ai_msg_t(content=""), generation_info={})]
    return ChatResult(generations=generations, llm_output=llm_output)


class ChatGoogleGenerativeAI(BaseChatModel):
    """`Google Generative AI` Chat models API.

    To use, you must have either:

        1. The ``GOOGLE_API_KEY``` environment variable set with your API key, or
        2. Pass your API key using the google_api_key kwarg to the ChatGoogle
           constructor.

    Example:
        .. code-block:: python

            from langchain_google_genai import ChatGoogleGenerativeAI
            chat = ChatGoogleGenerativeAI(model="gemini-pro")
            chat.invoke("Write me a ballad about LangChain")

    """

    model: str = Field(
        ...,
        description="""The name of the model to use.
Supported examples:
    - gemini-pro""",
    )
    max_output_tokens: int = Field(default=None, description="Max output tokens")

    client: Any  #: :meta private:
    google_api_key: Optional[SecretStr] = None
    temperature: Optional[float] = None
    """Run inference with this temperature. Must by in the closed
       interval [0.0, 1.0]."""
    top_k: Optional[int] = None
    """Decode using top-k sampling: consider the set of top_k most probable tokens.
       Must be positive."""
    top_p: Optional[float] = None
    """The maximum cumulative probability of tokens to consider when sampling.

        The model uses combined Top-k and nucleus sampling.

        Tokens are sorted based on their assigned probabilities so
        that only the most likely tokens are considered. Top-k
        sampling directly limits the maximum number of tokens to
        consider, while Nucleus sampling limits number of tokens
        based on the cumulative probability.

        Note: The default value varies by model, see the
        `Model.top_p` attribute of the `Model` returned the
        `genai.get_model` function.
    """
    n: int = Field(default=1, alias="candidate_count")
    """Number of chat completions to generate for each prompt. Note that the API may
       not return the full n completions if duplicates are generated."""
    convert_system_message_to_human: bool = False
    """Whether to merge any leading SystemMessage into the following HumanMessage.
    
    Gemini does not support system messages; any unsupported messages will 
    raise an error."""
    client_options: Optional[Dict] = Field(
        None,
        description="Client options to pass to the Google API client.",
    )
    transport: Optional[str] = Field(
        None,
        description="A string, one of: [`rest`, `grpc`, `grpc_asyncio`].",
    )

    class Config:
        allow_population_by_field_name = True

    @property
    def lc_secrets(self) -> Dict[str, str]:
        return {"google_api_key": "GOOGLE_API_KEY"}

    @property
    def _llm_type(self) -> str:
        return "chat-google-generative-ai"

    @property
    def _is_geminiai(self) -> bool:
        return self.model is not None and "gemini" in self.model

    @classmethod
    def is_lc_serializable(self) -> bool:
        return True

    @root_validator()
    def validate_environment(cls, values: Dict) -> Dict:
        """Validates params and passes them to google-generativeai package."""
        google_api_key = get_from_dict_or_env(
            values, "google_api_key", "GOOGLE_API_KEY"
        )
        if isinstance(google_api_key, SecretStr):
            google_api_key = google_api_key.get_secret_value()

        genai.configure(
            api_key=google_api_key,
            transport=values.get("transport"),
            client_options=values.get("client_options"),
        )
        if (
            values.get("temperature") is not None
            and not 0 <= values["temperature"] <= 1
        ):
            raise ValueError("temperature must be in the range [0.0, 1.0]")

        if values.get("top_p") is not None and not 0 <= values["top_p"] <= 1:
            raise ValueError("top_p must be in the range [0.0, 1.0]")

        if values.get("top_k") is not None and values["top_k"] <= 0:
            raise ValueError("top_k must be positive")
        model = values["model"]
        values["client"] = genai.GenerativeModel(model_name=model)
        return values

    @property
    def _identifying_params(self) -> Dict[str, Any]:
        """Get the identifying parameters."""
        return {
            "model": self.model,
            "temperature": self.temperature,
            "top_k": self.top_k,
            "n": self.n,
        }

    def _prepare_params(
        self, stop: Optional[List[str]], **kwargs: Any
    ) -> Dict[str, Any]:
        gen_config = {
            k: v
            for k, v in {
                "candidate_count": self.n,
                "temperature": self.temperature,
                "stop_sequences": stop,
                "max_output_tokens": self.max_output_tokens,
                "top_k": self.top_k,
                "top_p": self.top_p,
            }.items()
            if v is not None
        }
        if "generation_config" in kwargs:
            gen_config = {**gen_config, **kwargs.pop("generation_config")}
        params = {"generation_config": gen_config, **kwargs}
        return params

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        params, chat, message = self._prepare_chat(messages, stop=stop)
        response: genai.types.GenerateContentResponse = _chat_with_retry(
            content=message,
            **params,
            generation_method=chat.send_message,
        )
        return _response_to_result(response)

    async def _agenerate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        params, chat, message = self._prepare_chat(messages, stop=stop)
        response: genai.types.GenerateContentResponse = await _achat_with_retry(
            content=message,
            **params,
            generation_method=chat.send_message_async,
        )
        return _response_to_result(response)

    def _stream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        params, chat, message = self._prepare_chat(messages, stop=stop)
        response: genai.types.GenerateContentResponse = _chat_with_retry(
            content=message,
            **params,
            generation_method=chat.send_message,
            stream=True,
        )
        for chunk in response:
            _chat_result = _response_to_result(
                chunk,
                ai_msg_t=AIMessageChunk,
                human_msg_t=HumanMessageChunk,
                chat_msg_t=ChatMessageChunk,
                generation_t=ChatGenerationChunk,
            )
            gen = cast(ChatGenerationChunk, _chat_result.generations[0])
            yield gen
            if run_manager:
                run_manager.on_llm_new_token(gen.text)

    async def _astream(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        params, chat, message = self._prepare_chat(messages, stop=stop)
        async for chunk in await _achat_with_retry(
            content=message,
            **params,
            generation_method=chat.send_message_async,
            stream=True,
        ):
            _chat_result = _response_to_result(
                chunk,
                ai_msg_t=AIMessageChunk,
                human_msg_t=HumanMessageChunk,
                chat_msg_t=ChatMessageChunk,
                generation_t=ChatGenerationChunk,
            )
            gen = cast(ChatGenerationChunk, _chat_result.generations[0])
            yield gen
            if run_manager:
                await run_manager.on_llm_new_token(gen.text)

    def _prepare_chat(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        **kwargs: Any,
    ) -> Tuple[Dict[str, Any], genai.ChatSession, genai.types.ContentDict]:
        params = self._prepare_params(stop, **kwargs)
        history = _parse_chat_history(
            messages,
            convert_system_message_to_human=self.convert_system_message_to_human,
        )
        message = history.pop()
        chat = self.client.start_chat(history=history)
        return params, chat, message
