import os, uuid, datetime as dt
from flask import Flask, request, jsonify, abort
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import JSONB
from flask_cors import CORS

API_KEY = os.getenv("API_KEY", "")

def require_key():
    if API_KEY and request.headers.get("X-KEY") != API_KEY:
        abort(401)

def utcnow():
    return dt.datetime.utcnow()

db = SQLAlchemy()

def create_app():
    app = Flask(__name__)

    # psycopg3 URL normalization
    db_url = os.getenv("DATABASE_URL", "sqlite:///local.db")
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg://", 1)

    app.config["SQLALCHEMY_DATABASE_URI"] = db_url
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    CORS(app)
    db.init_app(app)

    @app.get("/ping")
    def ping():
        return {"ok": True, "time": utcnow().isoformat()}

    # ---------- MODELS ----------
    class EditorContent(db.Model):
        __tablename__ = "editor_contents"
        # location examples: "main", project/process ids, etc.
        location = db.Column(db.String(160), primary_key=True)
        content = db.Column(db.Text, nullable=False, default="")
        updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    class IndexEntry(db.Model):
        """
        One metadata row per entity.
        kind: 'project' | 'process' | 'list'
        container_id: parent entity (for lists under a project/process); NULL for containers and general lists
        """
        __tablename__ = "index_entries"
        id = db.Column(db.String, primary_key=True)
        kind = db.Column(db.String(16), nullable=False)  # project | process | list
        container_id = db.Column(db.String, db.ForeignKey("index_entries.id", ondelete="CASCADE"), nullable=True)
        name = db.Column(db.String(160), nullable=False)
        emoji = db.Column(db.String(8), nullable=True)
        color = db.Column(db.BigInteger, nullable=False, default=0xFF6AA6FF)
        order = db.Column(db.Integer, nullable=False, default=0, index=True)  # NEW
        opened_at = db.Column(db.DateTime, nullable=True, index=True)
        updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    class ListContent(db.Model):
        """
        Content for both containers and lists (1:1 with index_entries by id).
        container_id duplicated here for convenient filtering; cascades with parent.
        """
        __tablename__ = "list_contents"
        id = db.Column(db.String, db.ForeignKey("index_entries.id", ondelete="CASCADE"), primary_key=True)
        container_id = db.Column(db.String, db.ForeignKey("index_entries.id", ondelete="CASCADE"), nullable=True)
        order = db.Column(db.Integer, nullable=False, default=0, index=True)  # NEW
        content_json = db.Column(JSONB, nullable=False)
        updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    # ---------- Helpers ----------
    def idx_json(e: IndexEntry):
        return {
            "id": e.id,
            "kind": e.kind,
            "container_id": e.container_id,
            "name": e.name,
            "emoji": e.emoji,
            "color": e.color,
            "order": e.order,  # NEW
            "opened_at": e.opened_at.isoformat() if e.opened_at else None,
            "updated_at": e.updated_at.isoformat(),
        }

    def _next_order_for_container(container_id: str | None) -> int:
        """Return max(order)+1 among siblings (same container_id)."""
        max_ord = db.session.query(db.func.coalesce(db.func.max(IndexEntry.order), -1)) \
            .filter(IndexEntry.container_id == container_id).scalar()
        return int(max_ord) + 1

    # ---------- API ----------
    @app.get("/index")
    def get_index():
        """Fast list for the front menu (metadata only). Optional ?updated_since=ISO8601&sort=order"""
        require_key()
        since = request.args.get("updated_since")
        sort = request.args.get("sort", "updated_at")
        q = IndexEntry.query
        if since:
            ts = dt.datetime.fromisoformat(since.replace("Z", ""))
            q = q.filter(IndexEntry.updated_at > ts)
        if sort == "order":
            # Group by container, then stable by order, then updated_at for tiebreak
            q = q.order_by(IndexEntry.container_id.asc(), IndexEntry.order.asc(), IndexEntry.updated_at.desc())
        else:
            q = q.order_by(IndexEntry.updated_at.desc())
        rows = q.all()
        return jsonify([idx_json(e) for e in rows])

    @app.post("/entities")
    def create_entity():
        """
        Unified create for project/process/list.
        Body:
        {
          "kind": "project" | "process" | "list",
          "name": "...",
          "emoji": "…",
          "color": 4284287999,
          "order": 0,                  # optional; default append
          "content_json": { ... },
          "container_id": "UUID or null"
        }
        Rules:
          - project/process MUST have container_id = null
          - list MAY have container_id (nested) or null (general list)
        """
        require_key()
        d = request.get_json(force=True) or {}
        kind = d.get("kind")
        if kind not in ("project", "process", "list"):
            abort(400, "kind must be 'project' | 'process' | 'list'")
        name = d["name"]
        emoji = d.get("emoji")
        color = d.get("color", 0xFF6AA6FF)
        content_json = d.get("content_json", {})
        container_id = d.get("container_id")
        requested_order = d.get("order")

        if kind in ("project", "process") and container_id is not None:
            abort(400, "containers cannot have container_id")

        _id = str(uuid.uuid4())
        now = utcnow()

        effective_order = int(requested_order) if requested_order is not None else _next_order_for_container(container_id)

        idx = IndexEntry(
            id=_id, kind=kind, container_id=container_id, name=name, emoji=emoji,
            color=color, order=effective_order, opened_at=now, updated_at=now
        )
        db.session.add(idx)
        db.session.add(ListContent(id=_id, container_id=container_id, order=effective_order,
                                   content_json=content_json, updated_at=now))
        db.session.commit()
        return {"id": _id, "kind": kind, "container_id": container_id, "order": effective_order}, 201

    @app.delete("/entities/<entity_id>")
    def delete_entity(entity_id):
        """
        Delete any entity (project/process/list).
        - Deleting a container cascades to its child lists (index + content).
        """
        require_key()
        IndexEntry.query.filter_by(id=entity_id).delete(synchronize_session=False)
        db.session.commit()
        return {"ok": True}

    @app.get("/content/<entity_id>")
    def get_content(entity_id):
        """Fetch content_json for any entity."""
        require_key()
        row = ListContent.query.get_or_404(entity_id)
        return {
            "id": row.id,
            "container_id": row.container_id,
            "order": row.order,  # NEW
            "content_json": row.content_json,
            "updated_at": row.updated_at.isoformat()
        }

    @app.put("/content/<entity_id>")
    def update_content(entity_id):
        """Update content_json (and optionally order) and bump index.updated_at."""
        require_key()
        d = request.get_json(force=True) or {}
        if "content_json" not in d:
            abort(400, "content_json required")
        now = utcnow()
        row = ListContent.query.get_or_404(entity_id)
        row.content_json = d["content_json"]
        row.updated_at = now

        if "order" in d and d["order"] is not None:
            new_order = int(d["order"])
            row.order = new_order
            IndexEntry.query.filter_by(id=entity_id).update({"order": new_order})

        IndexEntry.query.filter_by(id=entity_id).update({"updated_at": now})
        db.session.commit()
        return {"ok": True, "updated_at": now.isoformat()}

    @app.put("/entities/<entity_id>")
    def update_entity_meta(entity_id):
        """
        Update metadata (name/emoji/color, mark opened).
        Body may include: name, emoji, color, mark_opened (bool), order (int)
        """
        require_key()
        d = request.get_json(force=True) or {}
        now = utcnow()

        updates = {}
        for k in ("name", "emoji", "color"):
            if k in d:
                updates[k] = d[k]
        if d.get("mark_opened"):
            updates["opened_at"] = now
        if "order" in d and d["order"] is not None:
            updates["order"] = int(d["order"])
            ListContent.query.filter_by(id=entity_id).update({"order": int(d["order"]), "updated_at": now})

        updates["updated_at"] = now

        changed = IndexEntry.query.filter_by(id=entity_id).update(updates)
        if not changed:
            abort(404)
        db.session.commit()
        return {"ok": True, "updated_at": now.isoformat()}

    @app.put("/entities/<entity_id>/order")
    def update_entity_order(entity_id):
        """Update only the ordering (kept in both tables). Body: {"order": int}"""
        require_key()
        d = request.get_json(force=True) or {}
        if "order" not in d:
            abort(400, "order required")
        new_order = int(d["order"])
        now = utcnow()
        changed = IndexEntry.query.filter_by(id=entity_id).update({"order": new_order, "updated_at": now})
        if not changed:
            abort(404)
        ListContent.query.filter_by(id=entity_id).update({"order": new_order, "updated_at": now})
        db.session.commit()
        return {"ok": True, "updated_at": now.isoformat(), "order": new_order}

    @app.get("/editor_content/<string:location>")
    def get_editor_content(location):
        require_key()
        row = EditorContent.query.get(location)
        if not row:
            abort(404)
        return {
            "location": row.location,
            "content": row.content,
            "updated_at": row.updated_at.isoformat()
        }

    @app.put("/editor_content/<string:location>")
    def put_editor_content(location):
        require_key()
        d = request.get_json(force=True) or {}
        content = d.get("content")
        if content is None or not isinstance(content, str):
            abort(400, "content (string) required")
        now = utcnow()
        row = EditorContent.query.get(location)
        if row:
            row.content = content
            row.updated_at = now
        else:
            row = EditorContent(location=location, content=content, updated_at=now)
            db.session.add(row)
        db.session.commit()
        return {"ok": True, "location": location, "updated_at": now.isoformat()}

    # ---------- Startup DDL (light migration) ----------
    with app.app_context():
        db.create_all()
        eng = db.engine
        try:
            with eng.connect() as cx:
                dialect = eng.dialect.name
                if dialect.startswith("postgres"):
                    cx.execute(db.text("""
                        -- existing columns/indexes
                        ALTER TABLE index_entries ADD COLUMN IF NOT EXISTS "order" INTEGER NOT NULL DEFAULT 0;
                        CREATE INDEX IF NOT EXISTS ix_index_entries_order ON index_entries ("order");
                        ALTER TABLE list_contents ADD COLUMN IF NOT EXISTS "order" INTEGER NOT NULL DEFAULT 0;
                        CREATE INDEX IF NOT EXISTS ix_list_contents_order ON list_contents ("order");

                        -- NEW: editor_contents table
                        CREATE TABLE IF NOT EXISTS editor_contents (
                            location   VARCHAR(160) PRIMARY KEY,
                            content    TEXT NOT NULL DEFAULT '',
                            updated_at TIMESTAMP NOT NULL
                        );
                        CREATE INDEX IF NOT EXISTS ix_editor_contents_updated_at
                            ON editor_contents (updated_at);
                    """))
                else:
                    # SQLite fallback: try-add columns and tables; ignore errors if they already exist
                    def _safe(sql: str):
                        try:
                            cx.execute(db.text(sql))
                        except Exception:
                            pass

                    _safe('ALTER TABLE index_entries ADD COLUMN "order" INTEGER NOT NULL DEFAULT 0;')
                    _safe('CREATE INDEX IF NOT EXISTS ix_index_entries_order ON index_entries ("order");')
                    _safe('ALTER TABLE list_contents ADD COLUMN "order" INTEGER NOT NULL DEFAULT 0;')
                    _safe('CREATE INDEX IF NOT EXISTS ix_list_contents_order ON list_contents ("order");')

                    # NEW: editor_contents table + index
                    _safe("""
                        CREATE TABLE IF NOT EXISTS editor_contents (
                            location   TEXT PRIMARY KEY,
                            content    TEXT NOT NULL DEFAULT '',
                            updated_at TEXT NOT NULL
                        );
                    """)
                    _safe("CREATE INDEX IF NOT EXISTS ix_editor_contents_updated_at ON editor_contents (updated_at);")
        except Exception as e:
            # Log but don’t block app start
            print("[migration warn]", e)
    return app

app = create_app()
# Run on Render: gunicorn app:app
