"""仅根据客户端消息标识识别新增事件，不按正文永久去重。"""

from collections import OrderedDict
from typing import Callable, Iterable, List

from wechat_forwarder import Message


class SnapshotTracker:
    """启动快照不补发；控件整体重建时重新建立基线，避免搬运历史。"""

    def __init__(self, log: Callable[[str], None] = print, capacity: int = 10000):
        if capacity <= 0:
            raise ValueError("快照缓存容量必须大于零")
        self.log = log
        self.capacity = capacity
        self._started = False
        self._previous = set()
        self._known = OrderedDict()

    def update(self, messages: Iterable[Message]) -> List[Message]:
        rows = list(messages)
        keys = [m.message_id for m in rows]
        if any(not key for key in keys) or len(keys) != len(set(keys)):
            raise ValueError("消息控件缺少唯一标识或标识重复，不能可靠识别新增消息")
        if len(rows) > self.capacity:
            raise ValueError("消息快照超过缓存上限，请缩小控件读取范围")
        current = set(keys)
        # 空快照不覆盖已有锚点，避免窗口短暂加载后把整页历史认成新消息。
        if not rows:
            return []
        baseline = not self._started
        if self._started and self._previous and not current.intersection(self._previous):
            baseline = True
            self.log("[适配] 消息列表与上一快照无重叠，已重新建立基线；请检查是否切群、滚动或客户端重载")
        # 向上滚动也可能保留部分旧控件。只接收上一帧最后一个可见锚点
        # 之后的追加消息，不能把新显露的历史行当成新到达的消息。
        anchor = max((index for index, key in enumerate(keys)
                      if key in self._previous), default=-1)
        result = [] if baseline else [m for m in rows[anchor + 1:]
                                      if m.message_id not in self._known]
        for key in keys:
            self._known[key] = None
            self._known.move_to_end(key)
        while len(self._known) > self.capacity:
            self._known.popitem(last=False)
        self._previous = current
        self._started = True
        return result
