import traceback
import logging

def get_error_info(e):
    error_info = traceback.format_exc()
    return error_info
