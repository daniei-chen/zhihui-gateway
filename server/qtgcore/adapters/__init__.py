"""适配器包。"""
from .doubaowork import DoubaoWorkAdapter
from .qoder import QoderAdapter
from .qoder2 import Qoder2Adapter
from .trae import TraeAdapter
from .trae2 import Trae2Adapter
from .workbuddy import WorkBuddyAdapter

__all__ = ["DoubaoWorkAdapter", "QoderAdapter", "Qoder2Adapter", "TraeAdapter",
           "Trae2Adapter", "WorkBuddyAdapter"]
