"""
dashboard/server.py
Flask 3.x + Flask-SocketIO 5.x dashboard server.
Runs on localhost:5000 and provides the control dashboard.

Routes:
  GET  /                → Dashboard SPA
  GET  /images/<file>   → Serve downloaded/posted images (also used by ngrok for IG hosting)
  GET  /api/status      → Automation + account status
  GET  /api/queue       → Current queue items
  GET  /api/history     → Published post history
  GET  /api/logs        → Recent log entries
  POST /api/control     → Start/stop automation
  POST /api/test-post   → Manual test publish
  GET|POST /api/settings → View/update settings
  GET  /api/duplicates  → Duplicate block history
  POST /api/retry-failed → Retry failed posts

Socket.IO events emitted to clients:
  'log'           → New log entry {level, message, source, time}
  'queue_update'  → Full queue list
  'status_update' → Automation state changed
"""

import hashlib
import os
import base64
import urllib.parse
from pathlib import Path

import requests
from flask import Flask, jsonify, render_template, request, send_from_directory, send_file
from flask_socketio import SocketIO, emit

# Module-level SocketIO instance (init_app pattern)
socketio = SocketIO()


def create_app(wa_monitor=None, queue_mgr=None, publisher=None) -> Flask:
    """
    Flask app factory.
    wa_monitor, queue_mgr, publisher are the singleton instances from their modules.
    """
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["SECRET_KEY"] = "wa-auto-publisher-dashboard-2024"

    # SQLAlchemy URI — using absolute path for reliability
    from app.database import get_app_root
    db_path = get_app_root() / "database" / "publisher.db"
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    # SocketIO with standard threading (better compatibility with Playwright/requests)
    socketio.init_app(
        app,
        async_mode="threading",
        cors_allowed_origins="*",
        logger=False,
        engineio_logger=False,
    )

    # Wire up SocketIO into the logger and queue manager
    from app.logger import app_logger
    app_logger.set_socketio(socketio)
    if queue_mgr:
        queue_mgr.set_socketio(socketio)

    # Store component references on app for use in routes
    app.wa_monitor = wa_monitor
    app.queue_mgr = queue_mgr
    app.publisher = publisher

    _register_routes(app)
    _register_socket_events()

    return app


def _register_routes(app: Flask) -> None:
    """Register all HTTP routes on the Flask app."""

    from app.config import APP_CONFIG
    from app.database import DuplicateBlock, Post, get_db
    from app.logger import app_logger

    # ── Static serving ──────────────────────────────────────────────────────

    @app.route("/images/<path:filename>")
    def serve_image(filename):
        """
        Serve image files from downloads/ or posted/.
        This route is also used by ngrok to provide public URLs for the IG API.
        """
        from app.database import get_data_dir
        root = get_data_dir()
        # Try downloads first
        if (root / "downloads" / filename).exists():
            return send_from_directory(str(root / "downloads"), filename)
        # Then posted
        if (root / "posted" / filename).exists():
            return send_from_directory(str(root / "posted"), filename)
        return jsonify({"error": "File not found"}), 404

    @app.route("/api/qr")
    def api_qr():
        """Serve the latest captured QR code for remote scanning."""
        from app.database import get_data_dir
        qr_path = get_data_dir() / "database" / "qr.png"
        if qr_path.exists():
            return send_file(str(qr_path), mimetype='image/png', max_age=0)
        return jsonify({"error": "No QR code available"}), 404

    # ── Main page ────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("index.html")

    # ── API: Status ──────────────────────────────────────────────────────────

    @app.route("/api/status")
    def api_status():
        wa_monitor = app.wa_monitor
        queue_mgr = app.queue_mgr
        publisher = app.publisher

        wa_status = wa_monitor.status if wa_monitor else {
            "running": False, "channel_url": "", "last_check": None
        }
        token_status = {"facebook": False, "instagram": False, "errors": ["Not configured"]}
        ig_quota = {"used": 0, "limit": 25, "remaining": 25}

        if publisher:
            try:
                token_status = publisher.verify_tokens()
                if token_status.get("instagram"):
                    ig_quota = publisher.check_ig_quota()
            except Exception:
                pass

        # Queue stats
        try:
            from app.database import SessionLocal
            sess = SessionLocal()
            try:
                pending_count = sess.query(Post).filter_by(status="pending").count()
                processing_count = sess.query(Post).filter_by(status="processing").count()
                posted_today = sess.query(Post).filter(
                    Post.status == "posted",
                    Post.posted_at >= __import__("datetime").datetime.utcnow().replace(
                        hour=0, minute=0, second=0
                    ),
                ).count()
                failed_count = sess.query(Post).filter_by(status="failed").count()
                total_posted = sess.query(Post).filter_by(status="posted").count()
            finally:
                sess.close()
        except Exception:
            pending_count = processing_count = posted_today = failed_count = total_posted = 0

        return jsonify({
            "automation_running": (
                wa_status.get("running", False) and
                (queue_mgr.is_running if queue_mgr else False)
            ),
            "whatsapp": wa_status,
            "accounts": token_status,
            "ig_quota": ig_quota,
            "channel_url": APP_CONFIG.get_wa_channel_url(),
            "configured": APP_CONFIG.is_configured(),
            "stats": {
                "pending": pending_count,
                "processing": processing_count,
                "posted_today": posted_today,
                "failed": failed_count,
                "total_posted": total_posted,
            },
        })

    # ── API: Queue ───────────────────────────────────────────────────────────

    @app.route("/api/queue")
    def api_queue():
        queue_mgr = app.queue_mgr
        if queue_mgr:
            return jsonify(queue_mgr.get_queue())
        return jsonify([])

    # ── API: History ─────────────────────────────────────────────────────────

    @app.route("/api/history")
    def api_history():
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 20, type=int)
        try:
            from app.database import SessionLocal
            sess = SessionLocal()
            try:
                total = sess.query(Post).filter_by(status="posted").count()
                posts = (
                    sess.query(Post)
                    .filter_by(status="posted")
                    .order_by(Post.posted_at.desc())
                    .offset((page - 1) * per_page)
                    .limit(per_page)
                    .all()
                )
                items = []
                for p in posts:
                    d = p.to_dict()
                    # Attach thumbnail
                    if p.image_path and os.path.exists(p.image_path):
                        try:
                            with open(p.image_path, "rb") as f:
                                d["thumbnail_b64"] = base64.b64encode(f.read()).decode()
                        except Exception:
                            d["thumbnail_b64"] = None
                    items.append(d)
                return jsonify({
                    "items": items,
                    "total": total,
                    "page": page,
                    "per_page": per_page,
                    "pages": (total + per_page - 1) // per_page,
                })
            finally:
                sess.close()
        except Exception as e:
            return jsonify({"items": [], "total": 0, "error": str(e)})

    # ── API: Logs ────────────────────────────────────────────────────────────

    @app.route("/api/logs")
    def api_logs():
        limit = request.args.get("limit", 200, type=int)
        from app.logger import app_logger
        return jsonify(app_logger.get_recent(limit))

    # ── API: Control ─────────────────────────────────────────────────────────

    @app.route("/api/control", methods=["POST"])
    def api_control():
        wa_monitor = app.wa_monitor
        queue_mgr = app.queue_mgr
        data = request.get_json(force=True) or {}
        action = data.get("action")

        if action == "start":
            if queue_mgr and not queue_mgr.is_running:
                queue_mgr.start()
            if wa_monitor and not wa_monitor.is_running:
                channel_url = APP_CONFIG.get_wa_channel_url()
                if channel_url and "YOUR_CHANNEL_ID" not in channel_url:
                    wa_monitor.start()
                else:
                    return jsonify({
                        "success": False,
                        "message": "WhatsApp channel URL not configured. Set it in Settings first."
                    })
            socketio.emit("status_update", {"running": True})
            return jsonify({"success": True, "message": "Automation started"})

        elif action == "stop":
            if wa_monitor:
                wa_monitor.stop()
            if queue_mgr:
                queue_mgr.stop()
            socketio.emit("status_update", {"running": False})
            return jsonify({"success": True, "message": "Automation stopped"})

        return jsonify({"success": False, "message": f"Unknown action: {action}"}), 400

    # ── API: Test post ────────────────────────────────────────────────────────

    @app.route("/api/test-post", methods=["POST"])
    def api_test_post():
        publisher = app.publisher
        if not publisher:
            return jsonify({"success": False, "error": "Publisher not initialised"}), 500

        if "image" not in request.files:
            return jsonify({"success": False, "error": "No image file provided"}), 400

        file = request.files["image"]
        caption = request.form.get("caption", "Test post from WA Channel Auto Publisher 🚀")
        publish_fb = request.form.get("facebook", "true").lower() == "true"
        publish_ig = request.form.get("instagram", "true").lower() == "true"

        # Save image temporarily
        from app.database import get_data_dir
        root = get_data_dir()
        img_bytes = file.read()
        img_hash = hashlib.sha256(img_bytes).hexdigest()[:16]
        ext = os.path.splitext(file.filename or "image.jpg")[1] or ".jpg"
        filename = f"test_{img_hash}{ext}"
        filepath = str(root / "downloads" / filename)
        with open(filepath, "wb") as f:
            f.write(img_bytes)

        results = {}
        if publish_fb:
            results["facebook"] = publisher.publish_to_facebook(filepath, caption)
        if publish_ig:
            results["instagram"] = publisher.publish_to_instagram(filepath, caption)

        overall_success = any(r.get("success") for r in results.values())
        return jsonify({"success": overall_success, "results": results})

    # ── API: Settings ────────────────────────────────────────────────────────

    @app.route("/api/settings", methods=["GET", "POST"])
    def api_settings():
        if request.method == "GET":
            return jsonify({
                "channel_url": APP_CONFIG.get_wa_channel_url(),
                "delay_seconds": APP_CONFIG.get_publish_delay(),
                "poll_interval": APP_CONFIG.get_poll_interval(),
                "publish_to_facebook": APP_CONFIG.get("publishing.publish_to_facebook", True),
                "publish_to_instagram": APP_CONFIG.get("publishing.publish_to_instagram", True),
                "app_id": APP_CONFIG.get_app_id(),
                "page_id": APP_CONFIG.get_page_id(),
                "ig_user_id": APP_CONFIG.get_ig_user_id(),
            })

        data = request.get_json(force=True) or {}

        # Plain settings
        if "channel_url" in data:
            APP_CONFIG.set("whatsapp.channel_url", data["channel_url"])
        if "delay_seconds" in data:
            APP_CONFIG.set("publishing.delay_seconds", int(data["delay_seconds"]))
        if "poll_interval" in data:
            APP_CONFIG.set("whatsapp.poll_interval_seconds", int(data["poll_interval"]))
        if "publish_to_facebook" in data:
            APP_CONFIG.set("publishing.publish_to_facebook", bool(data["publish_to_facebook"]))
        if "publish_to_instagram" in data:
            APP_CONFIG.set("publishing.publish_to_instagram", bool(data["publish_to_instagram"]))
        if "app_id" in data:
            APP_CONFIG.set("meta.app_id", data["app_id"])
        if "page_id" in data:
            APP_CONFIG.set("meta.page_id", data["page_id"])
        if "ig_user_id" in data:
            APP_CONFIG.set("meta.ig_user_id", data["ig_user_id"])

        # Encrypted tokens (only if non-empty)
        if data.get("page_access_token"):
            APP_CONFIG.set(
                "meta.page_access_token_encrypted",
                APP_CONFIG.encrypt_token(data["page_access_token"]),
            )
        if data.get("ig_access_token"):
            APP_CONFIG.set(
                "meta.ig_access_token_encrypted",
                APP_CONFIG.encrypt_token(data["ig_access_token"]),
            )
        if data.get("app_secret"):
            APP_CONFIG.set(
                "meta.app_secret_encrypted",
                APP_CONFIG.encrypt_token(data["app_secret"]),
            )
        if data.get("imgbb_api_key"):
            APP_CONFIG.set(
                "image_host.imgbb_api_key_encrypted",
                APP_CONFIG.encrypt_token(data["imgbb_api_key"]),
            )

        APP_CONFIG.save()
        app_logger.info("Settings updated via dashboard.", source="system")
        return jsonify({"success": True, "message": "Settings saved successfully"})

    # ── API: Duplicates ───────────────────────────────────────────────────────

    @app.route("/api/duplicates")
    def api_duplicates():
        try:
            from app.database import SessionLocal
            sess = SessionLocal()
            try:
                dups = (
                    sess.query(DuplicateBlock)
                    .order_by(DuplicateBlock.blocked_at.desc())
                    .limit(100)
                    .all()
                )
                return jsonify([d.to_dict() for d in dups])
            finally:
                sess.close()
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── API: Retry failed ─────────────────────────────────────────────────────

    @app.route("/api/retry-failed", methods=["POST"])
    def api_retry_failed():
        queue_mgr = app.queue_mgr
        if queue_mgr:
            queue_mgr.retry_failed()
            return jsonify({"success": True, "message": "Failed posts queued for retry"})
        return jsonify({"success": False, "error": "Queue manager not running"}), 500

    # ── API: Facebook OAuth ──────────────────────────────────────────────────

    @app.route("/api/auth/facebook/login")
    def api_auth_facebook_login():
        app_id = APP_CONFIG.get_app_id()
        if not app_id:
            return jsonify({"success": False, "error": "Meta App ID not configured"})
        
        # Dynamically build the redirect URI based on where the app is hosted (e.g. Railway or localhost)
        redirect_uri = urllib.parse.urljoin(request.host_url, "auth/facebook/callback")
        if "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
            redirect_uri = redirect_uri.replace("http://", "https://")
        scopes = "pages_manage_posts,pages_read_engagement,pages_show_list"
        auth_url = (
            f"https://www.facebook.com/v25.0/dialog/oauth?"
            f"client_id={app_id}&"
            f"redirect_uri={urllib.parse.quote(redirect_uri)}&"
            f"scope={scopes}&"
            f"response_type=code"
        )
        return jsonify({"success": True, "url": auth_url})

    @app.route("/auth/facebook/callback")
    def auth_facebook_callback():
        code = request.args.get("code")
        error = request.args.get("error")
        error_description = request.args.get("error_description")
        if error:
            return f"<h1>Facebook Login Failed</h1><p>{error_description}</p><a href='/'>Go back</a>"
        if not code:
            return "<h1>Error</h1><p>No authorization code received.</p><a href='/'>Go back</a>"
        app_id = APP_CONFIG.get_app_id()
        app_secret = APP_CONFIG.get_app_secret()
        redirect_uri = urllib.parse.urljoin(request.host_url, "auth/facebook/callback")
        if "localhost" not in redirect_uri and "127.0.0.1" not in redirect_uri:
            redirect_uri = redirect_uri.replace("http://", "https://")
        
        if not app_secret:
            return "<h1>Error</h1><p>App Secret is missing! Please save it in settings first.</p><a href='/'>Go back</a>"
        try:
            token_url = "https://graph.facebook.com/v25.0/oauth/access_token"
            resp = requests.get(token_url, params={
                "client_id": app_id,
                "redirect_uri": redirect_uri,
                "client_secret": app_secret,
                "code": code
            })
            resp.raise_for_status()
            short_token = resp.json().get("access_token")
            resp = requests.get(token_url, params={
                "grant_type": "fb_exchange_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "fb_exchange_token": short_token
            })
            long_token = resp.json().get("access_token", short_token)
            return f'''
            <html><body>
            <script>
                if (window.opener) {{
                    window.opener.postMessage({{type: "fb_login_success", token: "{long_token}"}}, "*");
                    window.close();
                }} else {{
                    localStorage.setItem("fb_user_token", "{long_token}");
                    window.location.href = "/";
                }}
            </script>
            <p>Login successful! You can close this window.</p>
            </body></html>
            '''
        except requests.exceptions.RequestException as e:
            detailed_err = str(e)
            if hasattr(e, "response") and e.response is not None:
                try:
                    detailed_err = e.response.json().get("error", {}).get("message", e.response.text)
                except Exception:
                    detailed_err = e.response.text
            return f"<h1>Error</h1><p>Failed to exchange token: {detailed_err}</p><a href='/'>Go back</a>"
        except Exception as e:
            return f"<h1>Error</h1><p>An unexpected error occurred: {e}</p><a href='/'>Go back</a>"

    @app.route("/api/auth/facebook/pages", methods=["POST"])
    def api_auth_facebook_pages():
        data = request.get_json(force=True) or {}
        user_token = data.get("user_token")
        if not user_token:
            return jsonify({"success": False, "error": "User token missing"})
        try:
            resp = requests.get("https://graph.facebook.com/v25.0/me/accounts", params={
                "access_token": user_token,
                "fields": "id,name,access_token"
            })
            resp.raise_for_status()
            pages_data = resp.json()
            pages = pages_data.get("data", [])
            
            # Fetch permissions for debugging
            perm_resp = requests.get("https://graph.facebook.com/v25.0/me/permissions", params={"access_token": user_token})
            perms_data = perm_resp.json() if perm_resp.ok else {}
            
            if not pages:
                return jsonify({"success": False, "error": f"API returned empty list. Data: {pages_data}, Perms: {perms_data}"})
                
            return jsonify({"success": True, "pages": pages, "debug_perms": perms_data})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    @app.route("/api/auth/facebook/save", methods=["POST"])
    def api_auth_facebook_save():
        data = request.get_json(force=True) or {}
        page_id = data.get("page_id")
        page_token = data.get("page_token")
        if not page_id or not page_token:
            return jsonify({"success": False, "error": "Missing page data"})
        APP_CONFIG.set("meta.page_id", page_id)
        APP_CONFIG.set("meta.page_access_token_encrypted", APP_CONFIG.encrypt_token(page_token))
        APP_CONFIG.save()
        app_logger.info(f"Facebook Page connected: {page_id}", source="system")
        return jsonify({"success": True, "message": "Facebook Page connected successfully!"})

    @app.route("/api/auth/instagram/accounts", methods=["POST"])
    def api_auth_instagram_accounts():
        page_id = APP_CONFIG.get_page_id()
        page_token = APP_CONFIG.get("meta.page_access_token_encrypted")
        if page_token:
            page_token = APP_CONFIG.decrypt_token(page_token)
            
        if not page_id or not page_token:
            return jsonify({"success": False, "error": "Please connect a Facebook Page first."})
        try:
            resp = requests.get(f"https://graph.facebook.com/v25.0/{page_id}", params={
                "fields": "instagram_business_account",
                "access_token": page_token
            })
            resp.raise_for_status()
            ig_account = resp.json().get("instagram_business_account")
            if not ig_account:
                 return jsonify({"success": False, "error": "No Instagram Business Account linked to the selected Facebook Page."})
            
            ig_id = ig_account.get("id")
            resp_ig = requests.get(f"https://graph.facebook.com/v25.0/{ig_id}", params={
                "fields": "id,username,name",
                "access_token": page_token
            })
            resp_ig.raise_for_status()
            ig_details = resp_ig.json()
            return jsonify({"success": True, "account": ig_details, "page_token": page_token})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)})

    @app.route("/api/auth/instagram/save", methods=["POST"])
    def api_auth_instagram_save():
        data = request.get_json(force=True) or {}
        ig_user_id = data.get("ig_user_id")
        ig_token = data.get("ig_token")
        if not ig_user_id or not ig_token:
            return jsonify({"success": False, "error": "Missing Instagram data"})
        APP_CONFIG.set("meta.ig_user_id", ig_user_id)
        APP_CONFIG.set("meta.ig_access_token_encrypted", APP_CONFIG.encrypt_token(ig_token))
        APP_CONFIG.save()
        app_logger.info(f"Instagram Business Account connected: {ig_user_id}", source="system")
        return jsonify({"success": True, "message": "Instagram connected successfully!"})

def _register_socket_events() -> None:
    """Register Socket.IO event handlers."""

    @socketio.on("connect")
    def on_connect():
        emit("connected", {"message": "Connected to WA Auto Publisher dashboard"})

    @socketio.on("disconnect")
    def on_disconnect():
        pass  # Client disconnected — no action needed

    @socketio.on("ping_status")
    def on_ping_status():
        """Client can request a status refresh."""
        emit("pong_status", {"ok": True})
