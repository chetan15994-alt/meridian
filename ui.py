"""Meridian design system — one place for icons and field labels so the look stays
consistent and is changeable in a single file.

- icon(name): maps a semantic name to a Streamlit Material icon token (":material/...:")
  which renders inside native widgets (buttons, headers, tabs). No decorative emojis.
- field(label, help): renders a field label with the hint inline (immediately next to the
  label) instead of relying on Streamlit's far-right `help=` tooltip.
"""
import streamlit as st

# Semantic name -> Streamlit Material Symbols token.
# Using a curated, consistent set keeps the UI production-clean.
_ICONS = {
    # navigation / tabs
    "review": "table_view", "analytics": "monitoring", "cv": "description",
    "settings": "settings", "tracker": "checklist",
    # actions
    "run": "play_arrow", "tailor": "auto_fix_high", "cover": "mail",
    "open": "open_in_new", "download": "download", "view": "visibility",
    "apply": "send", "add": "add", "save": "save", "test": "wifi_tethering",
    "delete": "delete", "restore": "history", "compare": "compare_arrows",
    "edit": "edit", "generate": "auto_awesome", "refresh": "refresh",
    "upload": "upload_file", "search": "search", "filter": "filter_list",
    # status
    "ok": "check_circle", "warn": "warning", "error": "error",
    "info": "info", "tip": "lightbulb", "key": "key", "lock": "lock",
    "web": "public", "company": "apartment", "location": "place",
    "money": "payments", "doc": "article", "score": "speed",
    "manual": "edit_note", "api": "bolt",
}

def icon(name):
    """Return a Streamlit Material icon token for use inside labels/buttons/headers."""
    return f":material/{_ICONS.get(name, 'circle')}:"

def field(label, help=None):
    """Render a field label with an inline hint directly beneath/next to the label,
    then return — call the input widget immediately after with label_visibility='collapsed'.
    Label/help are HTML-escaped: they feed an unsafe_allow_html markdown call.

    Usage:
        ui.field("Daily cost cap (USD)", "Tailoring stops once today's spend hits this.")
        cap = st.number_input("cap", ..., label_visibility="collapsed")
    """
    import html as _html
    label = _html.escape(str(label)); help = _html.escape(str(help)) if help else help
    if help:
        st.markdown(
            f"<div style='margin-bottom:-6px'><span style='font-weight:600'>{label}</span>"
            f"<span style='color:#888;font-size:0.85em'> &nbsp;·&nbsp; {help}</span></div>",
            unsafe_allow_html=True)
    else:
        st.markdown(f"<div style='margin-bottom:-6px;font-weight:600'>{label}</div>",
                    unsafe_allow_html=True)

def header(name, text, level=3):
    """Section header with a leading Material icon."""
    hashes = "#" * level
    st.markdown(f"{hashes} {icon(name)} {text}")
