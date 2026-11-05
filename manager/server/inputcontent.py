"""Bounded, content-free diagnostics for input that cannot be mapped faithfully."""


class UnsupportedContentError(ValueError):
    code='unsupported_content_type'

    def __init__(self, path):
        # Callers supply schema paths, never attachment contents or filenames.
        super().__init__('Unsupported input content block at '+path)
