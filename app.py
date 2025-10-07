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
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", "sqlite:///local.db")
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    CORS(app)
    db.init_app(app)

    @app.route("/ping")
    def ping():
        return {"ok": True, "time": utcnow().isoformat()}

    # ---------- MODELS ----------
    class Topic(db.Model):
        __tablename__ = "topics"
        id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))
        name = db.Column(db.String(120), nullable=False)
        emoji = db.Column(db.String(8), nullable=True)
        color = db.Column(db.Integer, nullable=False, default=0xFF6AA6FF)
        updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    class List(db.Model):
        __tablename__ = "lists"
        id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))
        topic_id = db.Column(db.String, db.ForeignKey("topics.id", ondelete="SET NULL"))
        name = db.Column(db.String(160), nullable=False)
        emoji = db.Column(db.String(8), nullable=True)
        color = db.Column(db.Integer, nullable=False, default=0xFF6AA6FF)
        opened_at = db.Column(db.DateTime, nullable=True, index=True)
        updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    class ListItem(db.Model):
        """
        Rich text content stored as JSON Delta (or your own shape)
        Example content_json: { "ops":[{"insert":"Hello"},{"attributes":{"bold":true},"insert":" world"}] }
        """
        __tablename__ = "list_items"
        id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))
        list_id = db.Column(db.String, db.ForeignKey("lists.id", ondelete="CASCADE"), index=True)
        order_idx = db.Column(db.Integer, nullable=False, default=0)
        content_json = db.Column(JSONB, nullable=False)
        updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)

    # ---------- HELPERS ----------
    def topic_json(t: Topic): return {
        "id": t.id, "name": t.name, "emoji": t.emoji, "color": t.color,
        "updated_at": t.updated_at.isoformat()
    }

    def list_meta_json(l: List): return {
        "id": l.id, "topic_id": l.topic_id, "name": l.name, "emoji": l.emoji,
        "color": l.color, "opened_at": l.opened_at.isoformat() if l.opened_at else None,
        "updated_at": l.updated_at.isoformat()
    }

    def item_json(i: ListItem): return {
        "id": i.id, "list_id": i.list_id, "order_idx": i.order_idx,
        "content_json": i.content_json, "updated_at": i.updated_at.isoformat()
    }

    # ---------- ROUTES ----------
    @app.post("/topics")
    def create_topic():
        require_key()
        data = request.get_json(force=True) or {}
        t = Topic(name=data["name"], emoji=data.get("emoji"), color=data.get("color", 0xFF6AA6FF))
        db.session.add(t); db.session.commit()
        return topic_json(t), 201

    @app.get("/topics")
    def get_topics():
        require_key()
        q = Topic.query.order_by(Topic.updated_at.desc()).all()
        return jsonify([topic_json(t) for t in q])

    @app.post("/lists")
    def create_list():
        require_key()
        d = request.get_json(force=True) or {}
        l = List(
            topic_id=d.get("topic_id"),
            name=d["name"], emoji=d.get("emoji"), color=d.get("color", 0xFF6AA6FF),
            opened_at=utcnow(), updated_at=utcnow()
        )
        db.session.add(l); db.session.commit()
        return list_meta_json(l), 201

    @app.get("/lists")
    def get_lists():
        require_key()
        # metadata only
        since = request.args.get("updated_since")
        q = List.query
        if since:
            q = q.filter(List.updated_at > dt.datetime.fromisoformat(since.replace("Z","")))
        q = q.order_by(List.updated_at.desc()).all()
        return jsonify([list_meta_json(l) for l in q])

    @app.put("/lists/<list_id>")
    def update_list(list_id):
        require_key()
        l = List.query.get_or_404(list_id)
        d = request.get_json(force=True) or {}
        if "name" in d: l.name = d["name"]
        if "emoji" in d: l.emoji = d["emoji"]
        if "color" in d: l.color = d["color"]
        if d.get("mark_opened"): l.opened_at = utcnow()
        l.updated_at = utcnow()
        db.session.commit()
        return list_meta_json(l)

    @app.delete("/lists/<list_id>")
    def delete_list(list_id):
        require_key()
        ListItem.query.filter_by(list_id=list_id).delete()
        List.query.filter_by(id=list_id).delete()
        db.session.commit()
        return {"ok": True}

    @app.get("/lists/<list_id>/items")
    def get_items(list_id):
        require_key()
        items = ListItem.query.filter_by(list_id=list_id).order_by(ListItem.order_idx.asc()).all()
        return jsonify([item_json(i) for i in items])

    @app.post("/lists/<list_id>/items")
    def add_item(list_id):
        require_key()
        d = request.get_json(force=True) or {}
        # expect content_json = { ...rich text... }
        max_idx = db.session.query(db.func.coalesce(db.func.max(ListItem.order_idx), -1)).filter_by(list_id=list_id).scalar()
        it = ListItem(list_id=list_id, order_idx=(max_idx + 1), content_json=d["content_json"], updated_at=utcnow())
        db.session.add(it)
        # bump list updated_at
        l = List.query.get(list_id)
        if l: l.updated_at = utcnow()
        db.session.commit()
        return item_json(it), 201

    @app.put("/lists/<list_id>/items/reorder")
    def reorder_items(list_id):
        require_key()
        d = request.get_json(force=True) or {}
        ids = d.get("item_ids", [])
        for idx, item_id in enumerate(ids):
            ListItem.query.filter_by(id=item_id, list_id=list_id).update({
                "order_idx": idx, "updated_at": utcnow()
            })
        l = List.query.get(list_id)
        if l: l.updated_at = utcnow()
        db.session.commit()
        return {"ok": True}

    @app.put("/lists/<list_id>/items/<item_id>")
    def update_item(list_id, item_id):
        require_key()
        d = request.get_json(force=True) or {}
        it = ListItem.query.filter_by(id=item_id, list_id=list_id).first_or_404()
        if "content_json" in d: it.content_json = d["content_json"]
        if "order_idx" in d: it.order_idx = int(d["order_idx"])
        it.updated_at = utcnow()
        l = List.query.get(list_id)
        if l: l.updated_at = utcnow()
        db.session.commit()
        return item_json(it)

    @app.get("/sync")
    def sync():
        """Return lists metadata and items updated since a timestamp (UTC ISO)."""
        require_key()
        since = request.args.get("updated_since")
        if not since:
            abort(400, "Provide updated_since=ISO8601")
        ts = dt.datetime.fromisoformat(since.replace("Z",""))
        lists = List.query.filter(List.updated_at > ts).all()
        items = ListItem.query.filter(ListItem.updated_at > ts).all()
        return {
            "lists": [list_meta_json(l) for l in lists],
            "items": [item_json(i) for i in items],
            "server_time": utcnow().isoformat()
        }

    with app.app_context():
        db.create_all()

    return app

app = create_app()

# Render runs via gunicorn: web: gunicorn app:app
