# Superset, plus the database drivers it deliberately does not ship.
#
# This is the ONE exception to "every dependency is a published image, used
# as-is", and it is worth naming rather than hiding. The rule exists so that
# nothing in this repo patches the emulator family or OpenMetadata to make a
# test pass -- a fork that makes the local stack agree with us about something
# production would not. This is a different thing: Apache Superset's own
# documentation states that its production image carries no database drivers
# and that installing the ones you need is the deployer's step. A hosted
# Superset in Azure needs exactly this layer.
#
# Nothing is patched. One `pip install` of pinned drivers, on a digest-pinned
# base. §20's 19c row adds the ODBC driver here for the Fabric warehouse.
FROM apache/superset:5.0.0@sha256:09735adaae3945c5a8fabbeca31a7954c8c74de195702ba61622aee8604d5c55

USER root
# `uv`, because that is what the image itself is built with -- its venv ships
# no pip at all, and adding one to install a driver would be a larger change
# to the image than the driver. psycopg2-binary rather than psycopg2 for the
# same reason: no build toolchain here, and adding gcc to compile a driver
# would again be the bigger edit.
RUN /usr/local/bin/uv pip install --python /app/.venv/bin/python3 \
      --no-cache psycopg2-binary==2.9.10
USER superset
