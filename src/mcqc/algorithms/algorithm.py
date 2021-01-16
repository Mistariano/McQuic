"""Base class of algorithms"""
import abc

from cfmUtils.base import Restorable


class Algorithm(Restorable, abc.ABC):
    """Algorithm base class."""
    @abc.abstractmethod
    def run(self, *args, **kwargs):
        """Main method"""
        raise NotImplementedError
