"""Moodle access via the official Web Services API (the same one the mobile app uses)."""
import base64
import re
import secrets
from urllib.parse import urlencode

import requests


class MoodleError(Exception):
    pass


def normalize_url(url: str) -> str:
    """https://moodle.x.ua/my/index.php → https://moodle.x.ua"""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    url = re.sub(r"\?.*$", "", url)
    url = re.sub(r"/(my|login|course|mod|user|admin|webservice)(/.*)?$", "", url)
    url = re.sub(r"/[^/]+\.php$", "", url)
    return url.rstrip("/")


def _json(r: requests.Response):
    try:
        return r.json()
    except ValueError:
        raise MoodleError(
            f"Moodle replied with a web page instead of data (address: {r.url}, code {r.status_code}). "
            "Check the address — only the site root is needed, e.g. https://moodle.univ.edu. "
            "If your university signs in via Microsoft/Google, use the token option."
        )


def google_login_link(base_url: str) -> str:
    """Sign-in link via Google (the same as in the Moodle mobile app)."""
    base_url = normalize_url(base_url)
    r = requests.post(f"{base_url}/lib/ajax/service-nologin.php?info=tool_mobile_get_public_config",
                      json=[{"index": 0, "methodname": "tool_mobile_get_public_config", "args": {}}], timeout=30)
    providers = re.findall(r"oauth2/login\.php\?id=(\d+)", str(_json(r)))
    params = {"service": "moodle_mobile_app", "passport": secrets.token_hex(8), "urlscheme": "moodlemobile"}
    if providers:
        params["oauthsso"] = providers[0]
    return f"{base_url}/admin/tool/mobile/launch.php?{urlencode(params)}"


def token_from_app_link(link: str) -> str:
    """moodlemobile://token=BASE64 → token."""
    m = re.search(r"token=([A-Za-z0-9+/=_-]+)", link.strip())
    if not m:
        raise MoodleError("Wrong link — it must start with moodlemobile://token=")
    raw = m.group(1)
    decoded = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode()
    parts = decoded.split(":::")
    if len(parts) < 2:
        raise MoodleError("Couldn't read the token from the link")
    return parts[1]


class Moodle:
    def __init__(self, base_url: str, token: str):
        self.base_url = normalize_url(base_url)
        self.token = token

    @staticmethod
    def login(base_url: str, username: str, password: str) -> str:
        """Get a token with a username and password."""
        r = requests.post(
            f"{normalize_url(base_url)}/login/token.php",
            data={"username": username, "password": password, "service": "moodle_mobile_app"},
            timeout=30,
        )
        data = _json(r)
        if "token" not in data:
            raise MoodleError(data.get("error") or str(data))
        return data["token"]

    def call(self, function: str, **params):
        r = requests.post(
            f"{self.base_url}/webservice/rest/server.php",
            data={"wstoken": self.token, "wsfunction": function, "moodlewsrestformat": "json", **params},
            timeout=60,
        )
        data = _json(r)
        if isinstance(data, dict) and "exception" in data:
            raise MoodleError(data.get("message") or str(data))
        return data

    def site_info(self):
        return self.call("core_webservice_get_site_info")

    def courses(self):
        uid = self.site_info()["userid"]
        return self.call("core_enrol_get_users_courses", userid=uid)

    def course_files(self, course_id: int):
        """Course sections with materials: [{section, modules: [{name, files: [{name, url, size}]}]}]."""
        result = []
        for section in self.call("core_course_get_contents", courseid=course_id):
            modules = []
            for module in section.get("modules", []):
                files = [{"name": c["filename"], "url": c["fileurl"], "size": c.get("filesize", 0)}
                         for c in module.get("contents") or [] if c.get("type") == "file"]
                if files:
                    modules.append({"name": module.get("name", ""), "files": files})
            if modules:
                result.append({"section": section.get("name") or "Untitled", "modules": modules})
        return result

    def download(self, file_url: str) -> bytes:
        sep = "&" if "?" in file_url else "?"
        r = requests.get(f"{file_url}{sep}token={self.token}", timeout=120)
        r.raise_for_status()
        return r.content
