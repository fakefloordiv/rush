import logging
from typing import Iterable
from traceback import format_exc

from rush.utils.exceptions import NotFound
from rush.core.entities import Handler, Request

logger = logging.getLogger(__name__)


class HandlersManager:
    def __init__(self, http_server, loader, handlers,
                 err_handlers, redirects):
        self.http_server = http_server
        self.loader = loader
        self.handlers = handlers
        self.err_handlers = err_handlers
        self.redirects = redirects  # from_path: response_with_new_path

        self.request_obj = Request(http_server, loader)
        # some hardcoded binds for err handlers
        self.not_found_handler = err_handlers['not-found']
        self.internal_error_handler = err_handlers['internal-error']

    def call_handler(self, body, conn, proto_version,
                     method, path, parameters, headers):
        request_obj = self.request_obj
        request_obj.build(protocol=proto_version,
                          method=method,
                          path=path,
                          parameters=parameters,
                          headers=headers,
                          body=body,
                          conn=conn,
                          file=None)

        if request_obj.path in self.redirects:
            return request_obj.raw_response(self.redirects[request_obj.path])

        handler = _pick_handler(self.handlers, request_obj)

        if handler is None:
            return self.not_found_handler(request_obj)

        try:
            handler.func(request_obj)
        except (FileNotFoundError, NotFound):
            self.not_found_handler(request_obj)
        except Exception as exc:
            logger.error('[ERROR-HANDLER] Caught an unhandled exception in handler (function name: '
                         f'{handler.func.__name__}): {exc}\nFull traceback:\n{format_exc()}')

            self.internal_error_handler(request_obj)


def err_handler_wrapper(err_handler_type, func, request):
    try:
        func(request)
    except Exception as exc:
        logger.error(f'caught an unhandled exception in {err_handler_type} handler (function name: '
                     f'{func.__name__}): {exc}\nFull traceback:\n{format_exc()}')


def _pick_handler(handlers: Iterable[Handler], request):
    acceptable_handler_paths = {request.path, '*'}

    for handler in handlers:
        if handler.path_route not in acceptable_handler_paths and not handler.any_paths:
            continue

        if request.method not in handler.methods:
            continue

        if handler.filter is not None and not handler.filter(request):
            continue

        return handler

    return None
