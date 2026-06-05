REQUIRED_STATIC_ASSETS: tuple[str, ...] = ("styles/app.css",)

REQUIRED_THEME_TOKENS: tuple[str, ...] = (
    "--web-core-colour-page-bg",
    "--web-core-colour-surface",
    "--web-core-colour-text",
    "--web-core-colour-muted-text",
    "--web-core-colour-border",
    "--web-core-colour-accent",
)

REQUIRED_THEME_SELECTORS: tuple[str, ...] = (
    'html[data-theme="light"]',
    'html[data-theme="dark"]',
    "@media (prefers-color-scheme: dark)",
)
