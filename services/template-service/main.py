from karnex_proxy import create_proxy_app

app = create_proxy_app(
    title="Karnex Template Service",
    service_id="template-service",
    path_prefixes=(
        "/job/",
        "/job",
        "/masters/opportunities",
        "/masters/customers",
    ),
)
