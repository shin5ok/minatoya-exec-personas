# ミナトヤ 幹部シミュレーター
# 使い方: make help

PROJECT        ?= $(or $(GOOGLE_CLOUD_PROJECT),$(shell gcloud config get-value project 2>/dev/null))
REGION         ?= us-central1
SERVICE        ?= minatoya-exec-personas
SA_NAME        ?= minatoya-exec-personas
SA_EMAIL       := $(SA_NAME)@$(PROJECT).iam.gserviceaccount.com
ARTIFACT_BUCKET ?= $(PROJECT)-minatoya-artifacts
REPO           ?= minatoya
IMAGE          ?= $(REGION)-docker.pkg.dev/$(PROJECT)/$(REPO)/$(SERVICE):latest
PORT           ?= 8000
# IAP でアクセスを許可する人（例: make iap-grant MEMBER=user:alice@example.com）
MEMBER         ?=
# Cloud Run に渡す環境変数（音声入力の on/off、時間の上限など）
VOICE_INPUT_ENABLED  ?= true
MAX_DURATION_MINUTES ?= 5
RUN_ENV := GOOGLE_GENAI_USE_ENTERPRISE=true,GOOGLE_CLOUD_PROJECT=$(PROJECT),GOOGLE_CLOUD_LOCATION=global,LIVE_LOCATION=$(REGION),GOOGLE_CLOUD_AGENT_ENGINE_LOCATION=$(REGION),LOGS_BUCKET_NAME=$(ARTIFACT_BUCKET),VOICE_INPUT_ENABLED=$(VOICE_INPUT_ENABLED),MAX_DURATION_MINUTES=$(MAX_DURATION_MINUTES)

.PHONY: wheels help install dev dev-agent-runtime playground test lint validate okf-index build setup deploy iap-grant url logs

help: ## ターゲットの一覧
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## 依存関係を入れる（uv）
	uv sync

dev: ## ローカルで起動（セッションはメモリ。http://localhost:8000）
	USE_IN_MEMORY_SESSION=true uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port $(PORT) --reload --reload-dir app --reload-dir frontend

dev-agent-runtime: ## ローカルで起動（セッションは Agent Runtime。初回は Runtime を作る）
	uv run uvicorn app.fast_api_app:app --host 127.0.0.1 --port $(PORT)

playground: ## ADK のプレイグラウンド（agents-cli）
	agents-cli playground

test: validate ## データ検証＋ユニットテスト
	uv run pytest tests/unit -q

lint: ## コードの静的チェック
	uv run --extra lint ruff check app tests scripts/okf.py
	node --check frontend/app.js && node --check frontend/audio.js

validate: ## データセットの整合性と OKF の適合性を確かめる
	uv run python scripts/validate.py
	uv run python scripts/okf.py lint

okf-index: ## knowledge/ の index.md を作り直す
	uv run python scripts/okf.py index

# wheel の取得元。別のインデックス（ミラーなど）を使うときは make wheels WHEEL_INDEX=... で上書きする
WHEEL_INDEX ?= https://pypi.org/simple/
wheels: ## コンテナ用の依存（Linux 用 wheel）を取得する（取得元は WHEEL_INDEX）
	uv export --frozen --no-dev --no-hashes --no-emit-project --format requirements-txt -o deployment/requirements.txt
	rm -rf deployment/wheels && mkdir -p deployment/wheels
	uv run --no-project --with pip python -m pip download -q --no-input -r deployment/requirements.txt \
	  -d deployment/wheels --index-url $(WHEEL_INDEX) --only-binary=:all: --python-version 3.12 --implementation cp \
	  --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 --platform linux_x86_64

build: wheels ## コンテナをビルドする（Cloud Build → Artifact Registry）
	gcloud builds submit --project $(PROJECT) --tag $(IMAGE) .

setup: ## 初回だけ: API の有効化、サービスアカウント、バケット、リポジトリを作る
	gcloud services enable run.googleapis.com iap.googleapis.com aiplatform.googleapis.com \
	  artifactregistry.googleapis.com cloudbuild.googleapis.com --project $(PROJECT)
	-gcloud iam service-accounts create $(SA_NAME) --project $(PROJECT) --display-name "Minatoya exec personas"
	for role in roles/aiplatform.user roles/storage.objectUser roles/logging.logWriter roles/cloudtrace.agent roles/telemetry.tracesWriter; do \
	  gcloud projects add-iam-policy-binding $(PROJECT) --member serviceAccount:$(SA_EMAIL) --role $$role --condition None >/dev/null; done
	gcloud projects add-iam-policy-binding $(PROJECT) --role roles/run.builder --condition None >/dev/null \
	  --member serviceAccount:$$(gcloud projects describe $(PROJECT) --format 'value(projectNumber)')-compute@developer.gserviceaccount.com
	-gcloud storage buckets create gs://$(ARTIFACT_BUCKET) --project $(PROJECT) --location $(REGION) --uniform-bucket-level-access
	-gcloud artifacts repositories create $(REPO) --project $(PROJECT) --location $(REGION) --repository-format docker

deploy: wheels ## Cloud Run にデプロイし、IAP を有効にする（agents-cli）
	agents-cli deploy --project $(PROJECT) --region $(REGION) --iap \
	  --service-account $(SA_EMAIL) --timeout 3600 --memory 2Gi --cpu 2 --concurrency 40 \
	  --env-vars "$(RUN_ENV)"
	gcloud run services add-iam-policy-binding $(SERVICE) --project $(PROJECT) --region $(REGION) \
	  --member serviceAccount:service-$$(gcloud projects describe $(PROJECT) --format 'value(projectNumber)')@gcp-sa-iap.iam.gserviceaccount.com \
	  --role roles/run.invoker

iap-grant: ## IAP で利用者を許可する（MEMBER=user:xxx@example.com または group:xxx）
	@test -n "$(MEMBER)" || (echo "MEMBER=user:xxx@example.com を指定してください" && exit 1)
	gcloud iap web add-iam-policy-binding --project $(PROJECT) --member $(MEMBER) \
	  --role roles/iap.httpsResourceAccessor --region $(REGION) --resource-type cloud-run --service $(SERVICE)

url: ## デプロイした URL を表示する
	@gcloud run services describe $(SERVICE) --project $(PROJECT) --region $(REGION) --format 'value(status.url)'

logs: ## Cloud Run のログを見る
	gcloud run services logs read $(SERVICE) --project $(PROJECT) --region $(REGION) --limit 100
