from karnex_proxy import create_proxy_app

app = create_proxy_app(
    title="Karnex Auth Service",
    service_id="auth-service",
    path_prefixes=("/auth",),
)
