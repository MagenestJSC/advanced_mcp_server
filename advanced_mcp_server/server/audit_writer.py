# Independent-cursor event log writes

import logging

from odoo import api
from odoo.http import request
from odoo.tools import config

_logger = logging.getLogger(__name__)


def push_event(uid, write_entry, failure_message, *args):
    if config["test_enable"]:
        try:
            event_log = api.Environment(request.env.cr, uid, {})["adv.event"].sudo()
            write_entry(event_log)
        except Exception:
            _logger.exception(failure_message, *args)
        return
    try:
        with request.env.registry.cursor() as cr:
            env = api.Environment(cr, uid, {})
            event_log = env["adv.event"].sudo()
            write_entry(event_log)
            cr.commit()
    except Exception:
        _logger.exception(failure_message, *args)
