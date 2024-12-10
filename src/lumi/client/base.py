from typing import Dict

from lumi.utils.common import decode_json
import logging

class StateCallbakcMixin:
    server_state: Dict

    def _on_state_callback(self, message):
        logging.debug("on_state_callback", message)
        self.server_state = decode_json(message.body)
