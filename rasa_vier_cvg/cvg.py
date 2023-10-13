import asyncio
import copy
from dataclasses import dataclass
import json
import base64
import logging
from functools import wraps
from typing import Any, Awaitable, Callable, Dict, Optional, Text, TypeVar
import warnings
import aiohttp

# ignore ResourceWarning, InsecureRequestWarning
warnings.filterwarnings("ignore", category=ResourceWarning)

from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse

import rasa.shared.utils.io
from rasa.core.channels.channel import InputChannel, OutputChannel, UserMessage

logger = logging.getLogger(__name__)

CHANNEL_NAME = "vier-cvg"
OPERATION_PREFIX = "cvg_"
DIALOG_ID_FIELD = "dialogId"

T = TypeVar('T')


def make_metadata(payload: T) -> Dict[str, T]:
    return {"cvg_body": payload}


@dataclass
class Recipient:
    dialog_id: str
    project_token: str
    reseller_token: str


def parse_recipient_id(recipient_id: Text) -> Recipient:
    parsed_json = json.loads(base64.b64decode(bytes(recipient_id, 'utf-8')).decode('utf-8'))
    project_context = parsed_json["projectContext"]
    return Recipient(parsed_json["dialogId"], project_context["projectToken"], project_context["resellerToken"])


class CVGOutput(OutputChannel):
    """Output channel for the Cognitive Voice Gateway"""

    on_message: Callable[[UserMessage], Awaitable[Any]]
    base_url: str
    proxy: Optional[str]

    @classmethod
    def name(cls) -> Text:
        return CHANNEL_NAME

    def __init__(self, callback_base_url: Text, on_message: Callable[[UserMessage], Awaitable[Any]], proxy: Optional[str] = None) -> None:
        self.on_message = on_message

        self.base_url = callback_base_url.rstrip('/')
        self.proxy = proxy

    async def _perform_request(self, path: str, method: str = "POST", data: Optional[any] = None) -> (int, Any):
        async with aiohttp.request(method, f"{self.base_url}{path}", json=data, proxy=self.proxy) as res:
            status = res.status
            if status == 204:
                return status, {}

            body = await res.json()
            if 200 <= status < 300:
                logger.error(f"Failed to send text message to CVG via /call/say: status={status}, body={body}")

            return status, body

    async def _say(self, dialog_id: str, text: str):
        await self._perform_request("/call/say", data={DIALOG_ID_FIELD: dialog_id, "text": text})

    async def send_text_message(self, recipient_id: Text, text: Text, **kwargs: Any) -> None:
        dialog_id = parse_recipient_id(recipient_id).dialog_id
        logger.info(f"Sending message to CVG dialog {dialog_id}: {text}")
        await self._say(dialog_id, text)

    async def _handle_refer_result(self, status_code: int, result: Dict, recipient_id: Text):
        if 200 <= status_code < 300:
            logger.info(f"Refer request succeeded: {status_code} with body {result}")
            return

        user_message = UserMessage(
            text="/cvg_refer_failure",
            output_channel=self,
            sender_id=recipient_id,
            input_channel=CHANNEL_NAME,
            metadata=make_metadata(result),
        )

        logger.info(f"Creating incoming UserMessage: text={user_message.text}, output_channel={user_message.output_channel}, sender_id={user_message.sender_id}, metadata={user_message.metadata}")
        await self.on_message(user_message)

    async def handle_bridge_result(self, status_code: int, result: Dict, recipient_id: Text):
        if not 200 <= status_code < 300:
            logger.info(f"Bridge request failed: {status_code} with body {result}")
            return

        status = result["status"]
        if status == "Success":
            user_message = UserMessage(
                text="/cvg_outbound_success",
                output_channel=self,
                sender_id=recipient_id,
                input_channel=CHANNEL_NAME,
                metadata=make_metadata(result),
            )
        elif status == "Failure":
            user_message = UserMessage(
                text="/cvg_outbound_failure",
                output_channel=self,
                sender_id=recipient_id,
                input_channel=CHANNEL_NAME,
                metadata=make_metadata(result),
            )
        else:
            logger.info(f"Invalid bridge result: {status}")
            return

        logger.info(f"Creating incoming UserMessage: text={user_message.text}, output_channel={user_message.output_channel}, sender_id={user_message.sender_id}, metadata={user_message.metadata}")
        await self.on_message(user_message)

    async def _execute_operation_by_name(self, operation_name: Text, body: Any, recipient_id: Text):
        recipient = parse_recipient_id(recipient_id)
        dialog_id = recipient.dialog_id
        reseller_token = recipient.reseller_token

        if body is None:
            newBody = {}
        else:
            newBody = copy.deepcopy(body)

        path = operation_name.replace("_", "/")
        if operation_name.startswith("call_"):
            if DIALOG_ID_FIELD not in newBody:
                newBody[DIALOG_ID_FIELD] = dialog_id

            status_code, response_body = await self._perform_request(path, data=newBody)

            # The response from forward and bridge must be handled
            handle_result_outbound_call_result_for = ["call_forward", "call_bridge"]
            if operation_name in handle_result_outbound_call_result_for:
                return self._handle_bridge_result(status_code, response_body)
            elif operation_name == 'call_refer':
                return self._handle_refer_result(status_code, response_body)
        elif operation_name.startswith("dialog_"):
            if operation_name == "dialog_delete":
                return self._perform_request(f"/dialog/{reseller_token}/{dialog_id}", method="DELETE", data=newBody)
            elif operation_name == "dialog_data":
                return self._perform_request(f"/dialog/{reseller_token}/{dialog_id}/data", data=newBody)
            else:
                logger.error(f"Dialog operation {operation_name} not found/not implemented yet. Consider using the cvg-python-sdk in your actions.")
        else:
            logger.error(f"Operation {operation_name} not found/not implemented yet. Consider using custom code in your actions.")
            return
        logger.info(f"Ran operation: {operation_name}")

    async def send_custom_json(self, recipient_id: Text, json_message: Dict[Text, Any], **kwargs: Any) -> None:
        logger.info(f"Received custom json: {json_message} to {recipient_id}")
        for operation_name, body in json_message.items():
            if operation_name[len(OPERATION_PREFIX):] == OPERATION_PREFIX:
                await self._execute_operation_by_name(operation_name[len(OPERATION_PREFIX):], body, recipient_id)

    async def send_image_url(*args: Any, **kwargs: Any) -> None:
        # We do not support images.
        rasa.shared.utils.io.raise_warning(
            "Ignoring image URL."
            "We cannot represent images as a voice bot."
            "Please define a voice-friendly alternative."
        )


class CVGInput(InputChannel):
    """Input channel for the Cognitive Voice Gateway"""

    callback: Optional[str]
    start_intent: str
    proxy: Optional[str]
    expected_authorization_header_value: str
    blocking_endpoints: bool

    @classmethod
    def name(cls) -> Text:
        return CHANNEL_NAME

    @classmethod
    def from_credentials(cls, credentials: Optional[Dict[Text, Any]]) -> InputChannel:
        if not credentials:
            cls.raise_missing_credentials_exception()
        token = credentials.get("token")
        if token is None or type(token) != str or len(token) == 0:
            raise ValueError('No authentication token has been configured in your credentials.yml!')
        proxy = credentials.get("proxy")
        start_intent = credentials.get("start_intent")
        if start_intent is None:
            start_intent = "/cvg_session"
        blocking_endpoints = credentials.get("blocking_endpoints")
        if blocking_endpoints is None:
            blocking_endpoints = True
        else:
            blocking_endpoints = bool(blocking_endpoints)

        logger.info(f"Creating input with: token={'*' * len(token)} proxy={proxy} start_intent={start_intent} blocking_endpoints={blocking_endpoints}")
        return cls(token, start_intent, proxy, blocking_endpoints)

    def __init__(self, token: Text, start_intent: Text, proxy: Optional[Text], blocking_endpoints: bool) -> None:
        self.callback = None
        self.expected_authorization_header_value = f"Bearer {token}"
        self.proxy = proxy
        self.start_intent = start_intent
        self.blocking_endpoints = blocking_endpoints

    async def process_message(self, request: Request, on_new_message: Callable[[UserMessage], Awaitable[Any]], text: Text, sender_id: Optional[Text]) -> Any:
        try:
            if text[-1] == ".":
                text = text[:-1]

            metadata = make_metadata(request.json)
            user_msg = UserMessage(
                text=text,
                output_channel=CVGOutput(request.json["callback"], on_new_message, self.proxy),
                sender_id=sender_id,
                input_channel=CHANNEL_NAME,
                metadata=metadata,
            )

            logger.info(f"Creating incoming UserMessage: text={text}, output_channel={user_msg.output_channel}, sender_id={sender_id}, metadata={metadata}")

            await on_new_message(user_msg)
        except Exception as e:
            logger.error(f"Exception when trying to handle message: {e}")
            logger.error(str(e), exc_info=True)

        return response.empty(204)

    def blueprint(self, on_new_message: Callable[[UserMessage], Awaitable[Any]]) -> Blueprint:
        def valid_request(func):
            def decorator(f):
                @wraps(f)
                async def decorated_function(request: HTTPResponse, *args, **kwargs):
                    if request.headers.get("authorization") != self.expected_authorization_header_value:
                        return response.text("bot token is invalid!", status=401)

                    if not request.headers.get("content-type") == "application/json":
                        return response.text("content-type is not supported. Please use application/json", status=415)
                    json_body = request.json
                    if json_body is None:
                        return response.text("body is not valid json.", status=400)
                    if json_body["dialogId"] is None:
                        return response.text("dialogId is required", status=400)
                    if json_body["callback"] is None:
                        return response.text("callback is required", status=400)
                    if json_body["projectContext"] is None:
                        return response.text("projectContext is required", status=400)
                    return await f(request, *args, **kwargs)
                return decorated_function
            return decorator(func)

        async def process_message_oneline(request: Request, text: Text, must_block: bool):
            sender_id_json=json.dumps({
                "dialogId": request.json["dialogId"],
                "projectContext": request.json["projectContext"],
            })
            sender_id_base64=base64.b64encode(bytes(sender_id_json, 'utf-8')).decode('utf-8')

            result = self.process_message(
                request,
                on_new_message,
                text=text,
                sender_id=sender_id_base64,
            )

            if self.blocking_endpoints or must_block:
                await result
            else:
                asyncio.create_task(result)

            return response.empty(204)
            
        cvg_webhook = Blueprint(
            "vier_cvg_webhook", __name__,
        )

        @cvg_webhook.post("/session")
        @valid_request
        async def session(request: Request) -> HTTPResponse:
            await process_message_oneline(request, self.start_intent, True)
            return response.json({"action": "ACCEPT"}, 200)
        
        @cvg_webhook.post("/message")
        @valid_request
        async def message(request: Request) -> HTTPResponse:
            return await process_message_oneline(request, request.json["text"], False)

        @cvg_webhook.post("/answer")
        @valid_request
        async def answer(request: Request) -> HTTPResponse:
            return await process_message_oneline(request, "/cvg_answer_" + request.json["type"]["name"].lower(), False)

        @cvg_webhook.post("/inactivity")
        @valid_request
        async def inactivity(request: Request) -> HTTPResponse:
            return await process_message_oneline(request, "/cvg_inactivity", False)

        @cvg_webhook.post("/terminated")
        @valid_request
        async def terminated(request: Request) -> HTTPResponse:
            return await process_message_oneline(request, "/cvg_terminated", False)

        @cvg_webhook.post("/recording")
        @valid_request
        async def recording(request: Request) -> HTTPResponse:
            return await process_message_oneline(request, "/cvg_recording", False)

        return cvg_webhook
