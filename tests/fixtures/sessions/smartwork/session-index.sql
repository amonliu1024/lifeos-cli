CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '未命名会话',
    customTitle TEXT,
    titleUpdatedAt REAL,
    lastAt REAL NOT NULL DEFAULT 0,
    lastMessageAt REAL NOT NULL DEFAULT 0,
    lastActivityAt REAL NOT NULL DEFAULT 0,
    workdir TEXT NOT NULL DEFAULT '',
    sessionPath TEXT NOT NULL DEFAULT '',
    isSubAgent INTEGER NOT NULL DEFAULT 0
);

INSERT INTO sessions VALUES (
    'sw-main', '合成主会话', '主会话自定义标题', NULL,
    1786176020000, 1786176020000, 1786176020000,
    '/synthetic/index-workspace',
    'sessions/2026/07/01/rollout-main.jsonl', 0
);
INSERT INTO sessions VALUES (
    'sw-child', '合成子会话', NULL, NULL,
    1786176030000, 1786176030000, 1786176030000,
    '/synthetic/index-workspace',
    'sessions/2026/08/08/rollout-child.jsonl', 1
);
INSERT INTO sessions VALUES (
    'sw-ordinary', '合成普通会话', NULL, NULL,
    1786176040000, 1786176040000, 1786176040000,
    '/synthetic/ordinary',
    'sessions/2026/08/08/rollout-ordinary.jsonl', 0
);
