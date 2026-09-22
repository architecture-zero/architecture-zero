# Shared by the two /api locations in nginx.conf - the general one and the
# exact-match upload one. Everything about reaching the backend lives here, so
# the two blocks differ in ONE directive (client_max_body_size) and cannot
# drift apart. Included, not duplicated.
#
# proxy_pass through a VARIABLE on purpose: with a literal upstream, nginx
# resolves the name at startup and REFUSES TO START when it cannot - so
# running this image without the backend beside it would fail as a container
# that will not boot rather than as a request that cannot reach its API. The
# variable defers resolution to request time, where it surfaces as a 502
# naming the upstream. 127.0.0.11 is docker's embedded DNS.
resolver 127.0.0.11 valid=10s ipv6=off;
set $az_backend http://backend:8000;
proxy_pass $az_backend$request_uri;

proxy_http_version 1.1;
# $http_host, NOT $host: $host drops the port, so a client on
# http://example:5173 reached the backend as Host: example while its browser
# sent Origin: http://example:5173. The backend's same-origin check compares
# those two, so the port-stripped Host made every same-origin request look
# cross-origin and the whole instance answered 403 anywhere but the default
# port. $http_host forwards the client's Host verbatim, which is what a
# same-origin comparison needs.
proxy_set_header Host              $http_host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;

# Chat answers stream token by token. With buffering on, nginx holds the whole
# response and the UI sits on a typing indicator until the answer is complete -
# the streaming works and looks broken.
proxy_buffering off;
proxy_cache off;
chunked_transfer_encoding on;
# Long enough for a slow local model to produce a first token.
proxy_read_timeout 300s;
