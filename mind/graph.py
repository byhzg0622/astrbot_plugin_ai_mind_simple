"""记忆图谱：把记忆库铺成一张能看的图。

和"小鲸鱼"那张图谱的区别在于：这里的节点不是额外抽出来的实体，
而是**直接从已有数据里长出来的**——

- 记忆本身
- 记忆的关键词
- 记忆挂在谁身上（owner_id）
- 记忆是在哪个会话里学到的（session_id）
- 两条记忆共享关键词 ⇒ 它们之间连一条"相似"边

聚类用的是"关键词当桥"的连通分量：共享同一个关键词的记忆自然聚成一团，
所以会出现一个大簇（日常聊的那些）+ 若干小簇（某次旅行、某个人、某件事）。
这跟人脑记东西的方式其实挺像的。

图是给浏览器画的，所以这里只负责出数据：节点、边、簇、统计。
力导向布局由前端算 —— 服务端不掺和布局，省 CPU 也省得卡聊天。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

#: 节点类型
NODE_MEMORY = "memory"
NODE_KEYWORD = "keyword"
NODE_USER = "user"
NODE_SESSION = "session"

#: 边的类型
EDGE_HAS = "has"          # 记忆 -> 关键词
EDGE_ABOUT = "about"      # 记忆 -> 人
EDGE_IN = "in"            # 记忆 -> 会话
EDGE_SIMILAR = "similar"  # 记忆 -> 记忆（共享关键词，一般不用画，靠关键词桥就够了）

DEFAULT_LIMIT = 260
MAX_LIMIT = 800
#: 关键词最多留这么多个（按出现次数切）
MAX_KEYWORDS = 220
#: 一个关键词连了太多记忆就不再当"桥"，否则整张图会被一个高频词拉成一大坨
MAX_KEYWORD_FANOUT = 18


@dataclass
class Node:
    id: str
    type: str
    label: str
    weight: float = 1.0
    cluster: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "weight": round(float(self.weight), 3),
            "cluster": self.cluster,
            "data": self.data,
        }


@dataclass
class Edge:
    source: str
    target: str
    kind: str = EDGE_HAS
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "weight": round(float(self.weight), 3),
        }


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}

    def add(self, item: str) -> None:
        self._parent.setdefault(item, item)

    def find(self, item: str) -> str:
        parent = self._parent.get(item, item)
        while parent != self._parent.get(parent, parent):
            parent = self._parent.get(parent, parent)
        # 路径压缩
        while self._parent.get(item, item) != parent:
            self._parent[item], item = parent, self._parent.get(item, item)
        return parent

    def union(self, left: str, right: str) -> None:
        self.add(left)
        self.add(right)
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            self._parent[root_right] = root_left


def _short(text: str, limit: int = 28) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"


def build_graph(
    store: Any,
    *,
    kinds: Sequence[str] = (),
    scopes: Sequence[str] = (),
    session_id: str = "",
    query: str = "",
    limit: int = DEFAULT_LIMIT,
    min_importance: float = 0.0,
    pinned_only: bool = False,
) -> dict[str, Any]:
    """从记忆库里拼出一张图。任何异常都降级成空图，不让面板崩。"""
    limit = max(10, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
    try:
        # 会话条件必须下推到 SQL：不然 LIMIT 会先把「所有会话里最新 N 条」
        # 取出来再过滤，选中会话的记忆一被截断，图上就是空的 ——
        # 表现成「图谱不跟顶部的会话选择器联动」。
        rows, total = store.query_page(
            q=query,
            scope=(scopes[0] if len(scopes) == 1 else ""),
            kind=(kinds[0] if len(kinds) == 1 else ""),
            pinned=True if pinned_only else None,
            session_id=session_id,
            limit=limit,
            offset=0,
        )
    except Exception:  # noqa: BLE001
        return _empty(total=0)

    memories = []
    for memory in rows or []:
        if session_id and str(getattr(memory, "session_id", "") or "") not in {session_id, ""}:
            # 只画选中会话的记忆，外加通用记忆（没绑会话的那些）。
            # 通用的一半要留着：手动加的关键词档案不该因为切了会话就消失。
            continue
        if kinds and str(getattr(memory, "kind", "") or "") not in set(kinds):
            continue
        if scopes and str(getattr(memory, "scope", "") or "") not in set(scopes):
            continue
        if float(getattr(memory, "importance", 0.0) or 0.0) < float(min_importance or 0.0):
            continue
        memories.append(memory)

    if not memories:
        return _empty(total=int(total or 0))

    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    keyword_nodes: dict[str, Node] = {}
    keyword_fanout: dict[str, int] = {}
    user_nodes: dict[str, Node] = {}
    session_nodes: dict[str, Node] = {}
    union = _UnionFind()

    for memory in memories:
        mid = f"m:{getattr(memory, 'id', 0)}"
        content = str(getattr(memory, "content", "") or "")
        node = Node(
            id=mid,
            type=NODE_MEMORY,
            label=_short(content),
            weight=max(0.25, float(getattr(memory, "importance", 0.5) or 0.5)),
            data={
                "content": content,
                "kind": str(getattr(memory, "kind", "") or ""),
                "scope": str(getattr(memory, "scope", "") or ""),
                "importance": round(float(getattr(memory, "importance", 0.0) or 0.0), 2),
                "pinned": bool(getattr(memory, "pinned", False)),
                "sensitive": bool(getattr(memory, "sensitive", False)),
                "hits": int(getattr(memory, "hit_count", 0) or 0),
                "session": str(getattr(memory, "session_id", "") or ""),
                "owner": str(getattr(memory, "owner_id", "") or ""),
                "created_at": round(float(getattr(memory, "created_at", 0.0) or 0.0), 2),
            },
        )
        nodes[mid] = node
        union.add(mid)

        for raw in tuple(getattr(memory, "keywords", ()) or ())[:8]:
            word = str(raw or "").strip()
            if not word or len(word) > 12:
                continue
            keyword_fanout[word] = keyword_fanout.get(word, 0) + 1
            kid = f"k:{word}"
            if kid not in keyword_nodes:
                keyword_nodes[kid] = Node(id=kid, type=NODE_KEYWORD, label=word, weight=1.0)
            keyword_nodes[kid].weight += 0.35
            keyword_nodes[kid].data["count"] = int(keyword_nodes[kid].data.get("count", 0)) + 1

        owner = str(getattr(memory, "owner_id", "") or "")
        if owner:
            uid = f"u:{owner}"
            if uid not in user_nodes:
                user_nodes[uid] = Node(id=uid, type=NODE_USER, label=owner, weight=1.4,
                                       data={"uid": owner})
            user_nodes[uid].weight += 0.2

        sess = str(getattr(memory, "session_id", "") or "")
        if sess:
            sid = f"s:{sess}"
            if sid not in session_nodes:
                session_nodes[sid] = Node(id=sid, type=NODE_SESSION, label=_short(sess, 22),
                                          weight=1.2, data={"session": sess})
            session_nodes[sid].weight += 0.15

    # 关键词当桥：连了太多记忆的高频词不参与聚类，免得整张图糊成一坨
    keep_keywords = set()
    for word, count in sorted(keyword_fanout.items(), key=lambda kv: -kv[1])[:MAX_KEYWORDS]:
        keep_keywords.add(word)

    for memory in memories:
        mid = f"m:{getattr(memory, 'id', 0)}"
        for raw in tuple(getattr(memory, "keywords", ()) or ())[:8]:
            word = str(raw or "").strip()
            if word not in keep_keywords:
                continue
            kid = f"k:{word}"
            edges.append(Edge(mid, kid, EDGE_HAS, 1.0))
            if keyword_fanout.get(word, 0) <= MAX_KEYWORD_FANOUT:
                union.union(mid, kid)
        owner = str(getattr(memory, "owner_id", "") or "")
        if owner:
            edges.append(Edge(mid, f"u:{owner}", EDGE_ABOUT, 0.8))
        sess = str(getattr(memory, "session_id", "") or "")
        if sess:
            edges.append(Edge(mid, f"s:{sess}", EDGE_IN, 0.5))

    nodes.update({kid: node for kid, node in keyword_nodes.items() if kid[2:] in keep_keywords})
    nodes.update(user_nodes)
    nodes.update(session_nodes)

    # 按连通分量分簇，大的排前面
    buckets: dict[str, list[str]] = {}
    for node in nodes.values():
        if node.type in (NODE_MEMORY, NODE_KEYWORD):
            buckets.setdefault(union.find(node.id), []).append(node.id)
    ordered = sorted(buckets.values(), key=len, reverse=True)
    clusters: list[dict[str, Any]] = []
    for index, members in enumerate(ordered, start=1):
        for node_id in members:
            if node_id in nodes:
                nodes[node_id].cluster = index
        words: dict[str, int] = {}
        for node_id in members:
            node = nodes.get(node_id)
            if node is not None and node.type == NODE_KEYWORD:
                words[node.label] = int(node.data.get("count", 1))
        top = [word for word, _count in sorted(words.items(), key=lambda kv: -kv[1])[:3]]
        clusters.append({
            "id": index,
            "name": "C/%02d" % index,
            "size": len([n for n in members if n in nodes and nodes[n].type == NODE_MEMORY]),
            "keywords": top,
        })

    # 单独的人和会话各自成簇，颜色才分得开
    offset = len(clusters)
    for node in list(user_nodes.values()) + list(session_nodes.values()):
        offset += 1
        node.cluster = offset
        clusters.append({
            "id": offset,
            "name": node.label,
            "size": 1,
            "keywords": [],
            "kind": node.type,
        })

    sessions = {str(getattr(m, "session_id", "") or "") for m in memories}
    sessions.discard("")
    return {
        "nodes": [node.to_dict() for node in nodes.values()],
        "edges": [edge.to_dict() for edge in edges],
        "clusters": clusters,
        "stats": {
            "memories_total": int(total or 0),
            "memories_shown": len(memories),
            "nodes": len(nodes),
            "edges": len(edges),
            "clusters": len(clusters),
            "sessions": len(sessions),
            "keywords": len(keep_keywords),
        },
        "filters": {
            "kinds": list(kinds),
            "scopes": list(scopes),
            "session": session_id,
            "query": query,
            "limit": limit,
            "min_importance": float(min_importance or 0.0),
            "pinned_only": bool(pinned_only),
        },
    }


def _empty(total: int = 0) -> dict[str, Any]:
    return {
        "nodes": [],
        "edges": [],
        "clusters": [],
        "stats": {
            "memories_total": int(total),
            "memories_shown": 0,
            "nodes": 0,
            "edges": 0,
            "clusters": 0,
            "sessions": 0,
            "keywords": 0,
        },
        "filters": {},
    }


__all__ = [
    "DEFAULT_LIMIT",
    "EDGE_ABOUT",
    "EDGE_HAS",
    "EDGE_IN",
    "EDGE_SIMILAR",
    "MAX_LIMIT",
    "NODE_KEYWORD",
    "NODE_MEMORY",
    "NODE_SESSION",
    "NODE_USER",
    "Edge",
    "Node",
    "build_graph",
]
