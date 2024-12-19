from scrapy.logformatter import LogFormatter
from scrapy.exceptions import DropItem
import logging


class SilentDropItem(DropItem):
    pass


class PoliteLogFormatter(LogFormatter):
    """
    Overwrite log level for silently dropped items
    Based on https://docs.scrapy.org/en/latest/_modules/scrapy/logformatter.html
    """

    def dropped(self, item, exception, response, spider):
        log_format = LogFormatter.dropped(self, item, exception, response, spider)
        if isinstance(exception, SilentDropItem):
            log_format["level"] = (
                logging.DEBUG
            )  # default is warning but I want to change it to debug
        return log_format
