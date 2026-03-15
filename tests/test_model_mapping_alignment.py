from iflow2api.anthropic_compat import get_mapped_model


def test_new_official_multimodal_aliases_are_preserved():
    assert get_mapped_model("qwen3-vl-plus", has_images=True) == "qwen3-vl-plus"
    assert get_mapped_model("qwen2.5-vl-72b-instruct", has_images=True) == "qwen2.5-vl-72b-instruct"
    assert get_mapped_model("qwen-vl-max-latest", has_images=True) == "qwen-vl-max-latest"
    assert get_mapped_model("nova-pro-v1", has_images=True) == "nova-pro-v1"
