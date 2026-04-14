"""
考研408每日真题推送插件
AstrBot 考研408历年真题插件

版本: 1.0.0
"""

from ._version import __version__, __plugin_name__, __plugin_desc__, __author__
from .main import Daily408Plugin

__all__ = ["Daily408Plugin", "__version__", "__plugin_name__", "__plugin_desc__", "__author__"]
