# Topic Views: Event Line and Relation Graph

## Product Shape

专题只保留两种主表达：

- `event_line`：按时间组织事件脉络，回答“发生了什么、先后关系是什么、最新变化是什么”。
- `relation_graph`：按主体和概念组织关系，回答“谁和谁有关、影响链路是什么、哪些变量连接在一起”。

这两种表达都必须能回到来源文章，避免生成内容不可追溯。

## Backend Contract

Endpoint:

```http
POST /api/topics/view
```

Request:

```json
{
  "topic": "张雪机车",
  "category_scope": ["sports"],
  "source_scope": null,
  "max_articles": 16
}
```

Response shape:

```json
{
  "topic": {
    "id": "topic_xxx",
    "title": "张雪机车",
    "status": "active",
    "updated_at": "..."
  },
  "event_line": {
    "view_type": "event_line",
    "lanes": [{"id": "origin", "label": "起点"}],
    "items": [
      {
        "id": "evt_xxx",
        "date": "2026-06-10",
        "title": "...",
        "summary": "...",
        "stage": "latest",
        "actors": [],
        "keywords": [],
        "confidence": 0.62,
        "source_article_ids": []
      }
    ]
  },
  "relation_graph": {
    "view_type": "relation_graph",
    "layout": "radial_seed",
    "nodes": [
      {"id": "topic", "label": "张雪机车", "type": "topic", "weight": 8, "source_article_ids": []}
    ],
    "edges": [
      {"id": "edge_xxx", "source": "topic", "target": "node_xxx", "label": "相关", "weight": 3, "source_article_ids": []}
    ]
  },
  "source_articles": []
}
```

## Current Builder

`TopicViewService` currently uses indexed/searchable articles and rule-based extraction:

- event line: one event per article, sorted by article time
- relation graph: topic node plus top entities/keywords; edges are topic relevance and article co-occurrence

This is intentionally a seed implementation. It gives the frontend a stable contract while the extraction quality can improve later.

## LLM Replacement Point

The LLM flow should replace only the extraction stage, not the frontend contract:

1. Retrieve topic evidence from ES and native ingestion.
2. Deduplicate evidence by URL/content hash.
3. Ask LLM to extract candidate events and entities with source ids.
4. Validate dates, source ids, entity names, and edge endpoints.
5. Merge with existing topic state.
6. Return the same `event_line` and `relation_graph` schema.

The LLM must not create events or relations without source evidence. If evidence is weak, return low confidence and keep the item visibly uncertain.

## Frontend

The Web prototype renders:

- `topic-room`: toggle between event line and relation graph.
- `event-map`: relation graph primary, latest event strip below.

Current rendering is lightweight SVG and HTML. The schema is compatible with later replacement by:

- vis-timeline style items/groups for event line.
- Cytoscape.js/D3 force layout style nodes/edges for relation graph.

## Library Notes

The current implementation does not require a heavy visualization dependency, but the contract follows common visualization models:

- Timeline libraries usually model events as `items` and optional `groups`, with time range, zoom, and item styling handled by the renderer.
- Graph libraries usually model `nodes`, `edges`, `layout`, and style separately, so extraction quality can evolve without changing the frontend contract.
- If the graph grows beyond the current radial seed layout, use a force or concentric layout and keep heavy layout work off the main interaction path.
