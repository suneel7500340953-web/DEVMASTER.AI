import io
import json
import re
import zipfile

import requests
import streamlit as st
import streamlit.components.v1 as components

# ---------- PAGE SETUP ----------
st.set_page_config(page_title="AI IDE & Project Generator", page_icon="💻", layout="wide")

# ---------- API CONFIG ----------
GROQ_BASE = "https://api.groq.com/openai/v1"
FALLBACK_MODELS = ["llama-3.3-70b-versatile", "qwen/qwen3-32b", "openai/gpt-oss-120b"]

try:
    API_KEY = st.secrets["GROQ_API_KEY"]
except Exception:
    st.error("GROQ_API_KEY missing hai. Streamlit secrets me `GROQ_API_KEY` add karein.")
    st.stop()


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_models(api_key: str) -> list[str]:
    """Groq se available chat models laata hai; fail hone par fallback list."""
    try:
        r = requests.get(
            f"{GROQ_BASE}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        r.raise_for_status()
        ids = [m["id"] for m in r.json().get("data", [])]
        skip = ("whisper", "tts", "guard", "playai", "embed", "distil")
        ids = sorted(i for i in ids if not any(s in i.lower() for s in skip))
        return ids or FALLBACK_MODELS
    except Exception:
        return FALLBACK_MODELS


# ---------- HELPERS ----------
def build_system_prompt(project_type: str) -> str:
    return (
        f"You are a Senior Developer. The user wants to build/edit a {project_type} project. "
        "If updating an existing project, modify the necessary files. "
        "For EVERY file you create or modify, you MUST use this exact format:\n"
        "### FILENAME: path/to/filename.ext\n"
        "```language\n[code here]\n```\n"
        "Output the complete content of each file. Do not skip files. "
        "Do not add conversational text, ONLY the file outputs."
    )


def strip_think(stream):
    """<think>...</think> reasoning blocks ko stream se hata deta hai."""
    open_tag, close_tag = "<think>", "</think>"
    buf, in_think = "", False
    for text in stream:
        buf += text
        out = ""
        while buf:
            if in_think:
                i = buf.find(close_tag)
                if i == -1:
                    buf = buf[-(len(close_tag) - 1):]
                    break
                buf = buf[i + len(close_tag):]
                in_think = False
            else:
                i = buf.find(open_tag)
                if i == -1:
                    keep = 0
                    for k in range(min(len(open_tag) - 1, len(buf)), 0, -1):
                        if open_tag.startswith(buf[-k:]):
                            keep = k
                            break
                    out += buf[: len(buf) - keep]
                    buf = buf[len(buf) - keep:]
                    break
                out += buf[:i]
                buf = buf[i + len(open_tag):]
                in_think = True
        if out:
            yield out
    if buf and not in_think:
        yield buf


def stream_groq(model: str, messages: list[dict]):
    """Groq se streaming response (sirf content text yield karta hai)."""
    resp = requests.post(
        f"{GROQ_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "stream": True},
        stream=True,
        timeout=120,
    )
    if resp.status_code != 200:
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"API error {resp.status_code}: {detail}")

    for line in resp.iter_lines():
        if not line:
            continue
        decoded = line.decode("utf-8")
        if not decoded.startswith("data: "):
            continue
        payload = decoded[6:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
            choices = chunk.get("choices") or []
            if not choices:
                continue
            content = choices[0].get("delta", {}).get("content")
            if content:
                yield content
        except json.JSONDecodeError:
            continue


def safe_path(name: str) -> str | None:
    """Path traversal / absolute paths rokta hai (ZIP aur editor dono ke liye)."""
    name = name.strip().strip("`").replace("\\", "/").lstrip("/")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


FILE_PATTERN = re.compile(
    r"###\s*FILENAME:\s*(.+?)\s*\n```[\w+\-.]*\n(.*?)\n?```", re.DOTALL
)


def build_api_messages(system_prompt: str, history: list[dict], files: dict) -> list[dict]:
    """History + current files ka context. Stored messages ko modify nahi karta."""
    api_messages = [{"role": "system", "content": system_prompt}]
    last_idx = len(history) - 1
    for i, msg in enumerate(history):
        content = msg["content"]
        if msg["role"] == "assistant" and msg.get("files"):
            # Purane turns me poora code dobara bhejne ki zarurat nahi (tokens bachte hain)
            content = "Updated files: " + ", ".join(msg["files"])
        if i == last_idx and msg["role"] == "user" and files:
            content += "\n\nHere are the current files in the project:\n"
            for fname, code in files.items():
                content += f"### FILENAME: {fname}\n```\n{code}\n```\n"
        api_messages.append({"role": msg["role"], "content": content})
    return api_messages


# ---------- SESSION STATE ----------
st.session_state.setdefault("messages", [])
st.session_state.setdefault("project_files", {})
st.session_state.setdefault("files_version", 0)  # editor refresh ke liye

# ---------- SIDEBAR ----------
with st.sidebar:
    st.header("Project Setup")
    project_type = st.selectbox(
        "Tech Stack:",
        [
            "HTML + Tailwind CSS Website",
            "Python Flask API",
            "Python Automation Script",
            "Next.js (React) App",
            "Node.js Express Backend",
        ],
    )
    models = fetch_models(API_KEY)
    model = st.selectbox("AI Model:", models)

    if st.button("🗑️ Reset IDE"):
        st.session_state.messages = []
        st.session_state.project_files = {}
        st.session_state.files_version += 1
        st.rerun()

col1, col2 = st.columns([1, 1.5])

# ---------- LEFT COLUMN: CHAT ----------
with col1:
    st.title("💻 AI Developer IDE")
    st.write("Build, Edit, and Preview your complete project.")

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if prompt := st.chat_input("Example: Ek login page banao..."):
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        api_messages = build_api_messages(
            build_system_prompt(project_type),
            st.session_state.messages,
            st.session_state.project_files,
        )

        with st.chat_message("assistant"):
            try:
                full_response = st.write_stream(strip_think(stream_groq(model, api_messages)))
            except Exception as e:
                st.error(f"Request fail ho gayi: {e}")
                st.session_state.messages.pop()  # failed prompt history se hatao
                st.stop()

        # Parse and update files
        updated = []
        for filename, code in FILE_PATTERN.findall(full_response):
            clean_name = safe_path(filename)
            if clean_name:
                st.session_state.project_files[clean_name] = code.strip()
                updated.append(clean_name)

        st.session_state.messages.append(
            {"role": "assistant", "content": full_response, "files": updated}
        )
        if updated:
            st.session_state.files_version += 1
            st.rerun()

# ---------- RIGHT COLUMN: IDE & PREVIEW ----------
with col2:
    files = st.session_state.project_files
    if files:
        # Download
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for fname, code in files.items():
                zf.writestr(fname, code)
        zip_buffer.seek(0)

        st.download_button(
            label="📦 Download Complete Project (.ZIP)",
            data=zip_buffer,
            file_name="ai_project.zip",
            mime="application/zip",
            type="primary",
        )

        tab_edit, tab_preview = st.tabs(["📝 Code Editor", "👁️ Live Preview"])

        with tab_edit:
            selected_file = st.selectbox("Select file to edit:", list(files.keys()))
            if selected_file:
                ver = st.session_state.files_version
                edited_code = st.text_area(
                    f"Editing: {selected_file}",
                    value=files[selected_file],
                    height=400,
                    key=f"editor_{selected_file}_{ver}",
                )
                if st.button("💾 Save Changes to File"):
                    st.session_state.project_files[selected_file] = edited_code
                    st.session_state.files_version += 1
                    st.success("File updated successfully!")
                    st.rerun()

        with tab_preview:
            html_file = next((f for f in files if f.endswith(".html")), None)
            if html_file:
                st.write(f"**Live Preview of `{html_file}`**")
                html_content = files[html_file]

                # CSS/JS files ko HTML me inline karo (href="style.css", "./style.css" dono)
                for fname, code in files.items():
                    base = fname.split("/")[-1]
                    if fname.endswith(".css"):
                        pattern = rf'<link[^>]*href=["\'](?:\./)?(?:{re.escape(fname)}|{re.escape(base)})["\'][^>]*>'
                        html_content = re.sub(
                            pattern, lambda m, c=code: f"<style>{c}</style>", html_content
                        )
                    elif fname.endswith(".js"):
                        pattern = rf'<script[^>]*src=["\'](?:\./)?(?:{re.escape(fname)}|{re.escape(base)})["\'][^>]*>\s*</script>'
                        html_content = re.sub(
                            pattern, lambda m, c=code: f"<script>{c}</script>", html_content
                        )

                components.html(html_content, height=500, scrolling=True)
            else:
                st.info(
                    "Live preview sirf HTML projects ke liye available hai. "
                    "Python/Node projects ko run karne ke liye ZIP download karein."
                )
    else:
        st.info("👈 Left side se project generate karein. Jaise hi code banega, yahan Editor aur Live Preview aayega!")
