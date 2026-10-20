"""Insight Harness — decision intelligence on a governed semantic layer.

Run:  streamlit run app.py
"""
from __future__ import annotations

import os

import streamlit as st

from harness import runtime_policy
from harness import semantic_layer as sl
from views import causal_studio, digest, help_page, home, monitoring, registry, reliability

st.set_page_config(page_title="Insight Harness", page_icon="🧭", layout="wide")

st.markdown("""
<style>
#MainMenu,
footer,
[data-testid="stToolbar"],
[data-testid="stDecoration"],
[data-testid="stStatusWidget"] {
    display: none !important;
}
[data-testid="stMainBlockContainer"] h1 {
    font-size: clamp(1.75rem, 2.4vw, 2.25rem);
    line-height: 1.15;
}
[data-testid="stSidebar"] h1 {
    font-size: 1.3rem;
    line-height: 1.2;
}
.tier-chip {display:inline-block; padding:2px 12px; border-radius:12px; color:white;
            font-size:0.78rem; font-weight:600; letter-spacing:.04em; margin-right:8px;}
.stamp {font-family:monospace; font-size:0.75rem; color:#6B7280; border-left:3px solid #0E7C7B;
        padding-left:10px; margin-top:4px;}
.caveat {background:#FDF6EC; border-left:3px solid #B07C0E; padding:8px 12px; margin:4px 0;
         font-size:0.86rem; border-radius:0 6px 6px 0;}
.refusal {background:#F1F2F4; border-left:3px solid #6B7280; padding:12px 14px;
          border-radius:0 6px 6px 0;}
</style>""", unsafe_allow_html=True)

PAGES = {
    "Home": home.render,
    "Digest": digest.render,
    "Monitoring": monitoring.render,
    "Causal Studio": causal_studio.render,
    "Semantic Layer": registry.render,
    "Reliability": reliability.render,
    "How answers are produced": help_page.render,
}

st.sidebar.title("Insight Harness")
page = st.sidebar.radio(
    "Navigate",
    list(PAGES),
    key="nav",
    label_visibility="collapsed",
)
st.sidebar.caption(f"Data version `{sl.data_version()}`")

session_key = st.session_state.get("api_key")
deployment_key = os.environ.get("ANTHROPIC_API_KEY")
deployment_allowed = bool(deployment_key and runtime_policy.deployment_llm_enabled())
credential_source = "session" if session_key else "deployment" if deployment_allowed else None

if credential_source == "session":
    translator_status = "language model · session credential"
elif credential_source == "deployment":
    translator_status = "language model · deployment credential"
else:
    translator_status = "built-in parser · ready"
st.sidebar.caption(f"Translator: {translator_status}")

with st.sidebar.expander("Connect a language model…", expanded=False):
    st.caption("Optional. The built-in parser keeps every page and governed calculation "
               "available without an API key.")
    st.caption("A language model only translates a question into a governed query; it never "
               "computes or writes the answer. Every translation is registry-validated, and "
               "invalid output falls back to the built-in parser.")
    if credential_source == "session":
        st.caption("Using the Anthropic API credential entered for this app session.")
    elif credential_source == "deployment":
        st.caption("Using an Anthropic API credential explicitly enabled by the deployment "
                   "owner for public sessions.")
    elif deployment_key:
        st.caption("A deployment credential exists but is not enabled for anonymous sessions. "
                   "Add your own key to use model-backed translation.")
    else:
        st.caption("Add your own Anthropic API key to use model-backed translation.")

    st.text_input("Anthropic API key", type="password", key="api_key",
                  help="Only enter a key in a deployment you trust.")
    if credential_source:
        models = runtime_policy.allowed_models()
        if st.session_state.get("llm_model") not in models:
            st.session_state["llm_model"] = models[0]
        st.selectbox("Model", models, key="llm_model",
                     help="The deployment owner controls this allowlist.")
        used = int(st.session_state.get("_model_calls_used", 0))
        limit = runtime_policy.session_model_call_limit()
        st.caption(f"Session model-call allowance: {max(0, limit - used)} of {limit} remaining.")
    st.caption("A key entered here remains in the current Streamlit session and is not written "
               "to disk by the app. The question and registered metric/dimension vocabulary "
               "are sent to Anthropic for intent translation; source rows are not. In a "
               "deployment you control, a server key also requires explicit public-use opt-in.")

PAGES[page]()
