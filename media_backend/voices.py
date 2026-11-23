"""Kokoro voices (no imports, so any module can use the list without a circular import)."""

VOICES = {
    "am_puck": "US male, young and bright", "am_liam": "US male, warm", "am_michael": "US male, deep",
    "am_adam": "US male, calm", "am_eric": "US male, clear", "af_heart": "US female, warm (best)", "af_bella": "US female, lively",
    "af_nicole": "US female, soft", "af_sarah": "US female, clear", "bf_emma": "UK female", "bf_alice": "UK female, bright",
    "bm_george": "UK male, deep", "bm_daniel": "UK male", "bm_lewis": "UK male, warm",
    "af_nova": "US female, bright", "af_sky": "US female, airy", "af_jessica": "US female, casual", "af_river": "US female, calm",
    "am_fenrir": "US male, strong", "am_onyx": "US male, low", "am_echo": "US male, smooth", "bf_isabella": "UK female, warm",
    "bf_lily": "UK female, young", "bm_fable": "UK male, storyteller",
    "ef_dora": "Spanish female", "em_alex": "Spanish male", "ff_siwis": "French female", "if_sara": "Italian female",
    "im_nicola": "Italian male", "pf_dora": "Portuguese (Brazil) female", "pm_alex": "Portuguese (Brazil) male",
    "hf_alpha": "Hindi female", "hm_omega": "Hindi male",
}


LANGS = {"a": "en-us", "b": "en-gb", "e": "es", "f": "fr-fr", "i": "it", "p": "pt-br", "h": "hi"}


def voice_lang(voice: str) -> str:
    """Kokoro voice names start with their language: a = US English, b = UK, e = Spanish, f = French..."""
    return LANGS.get(voice[:1], "en-us")
