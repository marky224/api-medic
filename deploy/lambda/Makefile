# SAM custom build for the hosted-demo Lambda.
#
# Why a Makefile rather than the default Python builder:
#   - We want to copy the api_medic source from src/api_medic/ into the
#     deployment zip rather than `pip install api-medic`, because the
#     installed package would drag its main deps (httpx, fastapi, etc).
#   - The lean dep set (pydantic + uncurl) lives in requirements.txt and
#     is installed normally.
#
# Invoked by `sam build` via Metadata.BuildMethod: makefile in template.yaml.
# Runs in a CI Linux environment — Mark doesn't need GNU make on Windows.

build-ApiAnalyzeFunction:
	pip install -r requirements.txt -t "$(ARTIFACTS_DIR)"
	cp -r api_medic "$(ARTIFACTS_DIR)/api_medic"
	cp handler.py "$(ARTIFACTS_DIR)/handler.py"
