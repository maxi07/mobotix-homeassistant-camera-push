from gunicorn.http.body import Body, EOFReader, LengthReader
from gunicorn.http.message import Message, Request


def is_mobotix_eof_request(request) -> bool:
    headers = dict(request.headers)
    return (
        request.version == (1, 0)
        and request.method == "POST"
        and headers.get("CONNECTION", "").lower() != "keep-alive"
        and headers.get("USER-AGENT", "").lower().startswith("mxmsg/")
    )


def set_compatible_body_reader(request) -> None:
    Message.set_body_reader(request)
    if isinstance(request.body.reader, EOFReader):
        if is_mobotix_eof_request(request):
            return
        request.body = Body(LengthReader(request.unreader, 0))


Request.set_body_reader = set_compatible_body_reader
