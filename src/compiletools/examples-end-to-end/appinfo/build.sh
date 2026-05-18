#!/usr/bin/bash
# Build the appinfo example using the preferred name/version pattern.
#
# gen_appinfo.sh writes appinfo.cpp next to appinfo.hpp, so ct-cake's
# implied-source mechanism picks it up via --auto without an explicit
# target listing. To override the baked-in values, export APP_NAME /
# APP_VERSION before invoking; the defaults come from `git describe`
# plus a literal app name.

ct-cake --auto \
    --prebuild-script='./gen_appinfo.sh appinfo.cpp' \
    "$@"

./bin/*/main
