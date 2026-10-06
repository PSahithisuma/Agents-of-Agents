FROM grafana/grafana:10.4.0
COPY infra/grafana/provisioning /etc/grafana/provisioning
COPY infra/grafana/dashboards    /etc/grafana/dashboards
ENV GF_SERVER_HTTP_PORT=3000
ENV GF_PATHS_DATA=/tmp/grafana-data
ENV GF_PATHS_LOGS=/tmp/grafana-logs
ENV GF_PATHS_PLUGINS=/tmp/grafana-plugins