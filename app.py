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
        color = db.Column(db.Integer, nullable=False, default=0xFF6AA6FF)
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
            "opened_at": e.opened_at.isoformat() if e.opened_at else None,
            "updated_at": e.updated_at.isoformat(),
        }

    # ---------- API ----------
    @app.get("/index")
    def get_index():
        """Fast list for the front menu (metadata only). Optional ?updated_since=ISO8601"""
        require_key()
        since = request.args.get("updated_since")
        q = IndexEntry.query
        if since:
            ts = dt.datetime.fromisoformat(since.replace("Z", ""))
            q = q.filter(IndexEntry.updated_at > ts)
        q = q.order_by(IndexEntry.updated_at.desc()).all()
        return jsonify([idx_json(e) for e in q])

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

        if kind in ("project", "process") and container_id is not None:
            abort(400, "containers cannot have container_id")

        _id = str(uuid.uuid4())
        now = utcnow()

        idx = IndexEntry(
            id=_id, kind=kind, container_id=container_id, name=name, emoji=emoji,
            color=color, opened_at=now, updated_at=now
        )
        db.session.add(idx)
        db.session.add(ListContent(id=_id, container_id=container_id, content_json=content_json, updated_at=now))
        db.session.commit()
        return {"id": _id, "kind": kind, "container_id": container_id}, 201

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
            "content_json": row.content_json,
            "updated_at": row.updated_at.isoformat()
        }

    @app.put("/content/<entity_id>")
    def update_content(entity_id):
        """Update content_json (and bump index.updated_at)."""
        require_key()
        d = request.get_json(force=True) or {}
        if "content_json" not in d:
            abort(400, "content_json required")
        now = utcnow()
        row = ListContent.query.get_or_404(entity_id)
        row.content_json = d["content_json"]
        row.updated_at = now
        IndexEntry.query.filter_by(id=entity_id).update({"updated_at": now})
        db.session.commit()
        return {"ok": True, "updated_at": now.isoformat()}

    @app.put("/entities/<entity_id>")
    def update_entity_meta(entity_id):
        """
        Update metadata (name/emoji/color, mark opened).
        Body may include: name, emoji, color, mark_opened (bool)
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
        updates["updated_at"] = now

        changed = IndexEntry.query.filter_by(id=entity_id).update(updates)
        if not changed:
            abort(404)
        db.session.commit()
        return {"ok": True, "updated_at": now.isoformat()}

    with app.app_context():
        db.create_all()

    return app

app = create_app()
# Run on Render: gunicorn app:app

