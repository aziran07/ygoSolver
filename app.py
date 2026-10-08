"""Yu-Gi-Oh! deck photo -> card list (Streamlit). Run: streamlit run app.py"""

import hashlib
import io
import uuid

import streamlit as st
from PIL import Image, ImageDraw, ImageOps

import catalog
import exports
import rarities
import recognition
import references

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 40_000_000
LANGUAGES = {"한국어": "name_ko", "일본어": "name_ja", "영어": "name_en"}

st.set_page_config(page_title="유희왕 덱 인식", layout="wide")
state = st.session_state
state.setdefault("rows", [])
state.setdefault("recognized_signature", None)
state.setdefault("recognition_error", None)
state.setdefault("overlays", [])
state.setdefault("export_fingerprint", None)
state.setdefault("card_lookups", {})  # normalized English name -> {"card": ...} or {"error": ...}
state.setdefault("rarity_lookups", {})  # official CID -> {"rarities": [...]} or {"error": ...}


@st.cache_resource(show_spinner=False)
def get_recognizer():
    return recognition.Recognizer()


@st.cache_data(max_entries=20, show_spinner=False)
def load_image(data):
    """Return an EXIF-rotated RGB image; raise ValueError with the reason when rejected."""
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError(f"파일이 너무 큽니다 ({len(data) / 1024 / 1024:.1f}MB, 최대 20MB)")
    image = Image.open(io.BytesIO(data))
    width, height = image.size  # read from the header, before decoding pixels
    if width * height > MAX_PIXELS:
        raise ValueError(f"해상도가 너무 큽니다 ({width}x{height}, 최대 4천만 화소)")
    return ImageOps.exif_transpose(image).convert("RGB")


def lookup(name_en):
    """Official metadata for an English name as {"card": ...} or {"error": ...}.

    Successes and failures are both kept for the session, so reruns and repeated cards do not
    hit the network again; failures are retried only by the explicit retry button.
    """
    key = catalog.normalize_name(name_en)
    if key not in state.card_lookups:
        try:
            state.card_lookups[key] = {"card": catalog.resolve_card(name_en)}
        except Exception as error:  # shown on the row; the row stays unresolved
            state.card_lookups[key] = {"error": f"공식 DB 조회 실패 ({name_en}): {type(error).__name__}: {error}"}
    return state.card_lookups[key]


def rarity_lookup(cid):
    """Official rarity codes (lowest first) for a CID as {"rarities": [...]} or {"error": ...}.

    A confirmed absence of any Korean/Japanese printing is {"rarities": [], "absent": reason}: an expected
    state, not a failure, so it offers no retry. Cached for the session like card lookups; failures are
    retried only by the explicit retry button.
    """
    if cid not in state.rarity_lookups:
        try:
            state.rarity_lookups[cid] = {"rarities": list(rarities.get_rarities(cid))}
        except rarities.RarityUnavailable as absence:
            state.rarity_lookups[cid] = {"rarities": [], "absent": str(absence)}
        except Exception as error:  # shown on the row; export stays blocked
            state.rarity_lookups[cid] = {"error": f"레어도 조회 실패 (CID {cid}): {type(error).__name__}: {error}"}
    return state.rarity_lookups[cid]


def is_reference(candidate):
    return candidate.get("method") == "reference"


def is_reviewed(candidate):
    """A model candidate compared with its own official artworks (references.CandidateReviewer)."""
    return "review" in candidate


def resolve_into(row, candidate):
    """Select a candidate for a row. A reference match or a reviewed candidate carries its own
    validated official card; a plain model candidate takes its official metadata from the session lookup."""
    if is_reference(candidate):
        row.update(choice=candidate["card_id"], card=candidate["reference_card"], error=None)
        return
    if is_reviewed(candidate):
        row.update(choice=candidate["card_id"], card=candidate["review"]["card"], error=None)
        return
    result = lookup(candidate["name_en"])
    row.update(choice=candidate["card_id"], card=result.get("card"), error=result.get("error"))


def reference_candidate(match):
    """First candidate for a crop that matched an official reference image (evidence, no probability)."""
    card = match["card"]
    if not (card["name_ko"] or card["name_ja"] or card["name_en"]):
        raise ValueError(f"참조 라이브러리의 CID {card['cid']} 카드에 공식 이름이 하나도 없습니다 (잘못된 메타데이터)")
    return {"card_id": f"official:{card['cid']}", "method": "reference", "reference_card": card,
            "name_en": card["name_en"], "inliers": match["inliers"], "inlier_ratio": match["inlier_ratio"],
            "spread": match["spread"], "margin": match["margin"]}


def with_reference_first(reference, model_candidates):
    """Reference candidate first, then model proposals that are not the same English identity, max 3."""
    if reference["name_en"] is None:
        others = model_candidates
    else:
        same = catalog.normalize_name(reference["name_en"])
        others = [c for c in model_candidates if catalog.normalize_name(c["name_en"]) != same]
    return [reference, *others][:3]


def candidate_label(candidate):
    """Official Korean name, else official Japanese name when Korean is confirmed absent; never English."""
    if is_reference(candidate):
        card = candidate["reference_card"]
        name = card["name_ko"] or card["name_ja"] or f"한국어·일본어 공식 이름 없음 (공식 CID {card['cid']})"
        return f"{name} (공식 이미지 일치 · 특징점 {candidate['inliers']}개)"
    if is_reviewed(candidate):
        review = candidate["review"]
        card = review["card"]
        name = card["name_ko"] or card["name_ja"] or f"한국어·일본어 공식 이름 없음 (공식 CID {card['cid']})"
        evidence = {"verified": "일러스트 검증됨", "leading": "일러스트 근거 약함 · 확인 필요",
                    "none": "일러스트 근거 없음"}[review["outcome"]]
        return f"{name} (모델 {candidate['score']:.2%} · {evidence} · 특징점 {review['inliers']}개)"
    result = lookup(candidate["name_en"])
    if "error" in result:
        name = f"공식 이름 조회 실패 (DRAW2 ID {candidate['card_id']})"
    else:
        card = result["card"]
        name = card["name_ko"] or card["name_ja"] or f"한국어·일본어 공식 이름 없음 (DRAW2 ID {candidate['card_id']})"
    return f"{name} ({candidate['score']:.0%})"


def export_name(row, language_key):
    if row["name_override"].strip():
        return row["name_override"].strip()
    return (row["card"] or {}).get(language_key) or ""


def problems_of(row, language_key):
    if row["card"] is None:
        return row["error"] or "후보를 선택하세요 (자동 확정되지 않은 카드)"
    if not export_name(row, language_key):
        return "선택한 언어의 공식 이름이 없습니다. 내보낼 표시 이름을 직접 입력하거나 언어를 바꾸세요."
    result = rarity_lookup(row["card"]["cid"])
    if "error" in result:
        return result["error"]
    if not result["rarities"]:
        detail = f" ({result['absent']})" if "absent" in result else ""
        return ("공식 DB에 이 카드의 한국어·일본어(OCG) 판본이 없어 고를 수 있는 레어도가 없습니다. "
                "이 카드는 내보낼 수 없으므로 삭제하거나 다른 후보를 고르세요." + detail)
    if row.get("rarity_cid") != row["card"]["cid"] or row.get("rarity") is None:
        return "레어도를 선택하세요."
    if row["rarity"] not in result["rarities"]:
        return f"선택했던 레어도 {row['rarity']!r}가 이 카드의 공식 레어도 목록에 없습니다. 레어도를 다시 선택하세요."
    return None


def recognition_summary(recognized_rows, overlays, used_references):
    """Return (summary text, whether every detected card is confirmed) for the recognized rows."""
    confirmed_count = sum(row["status"] == "recognized" and row["card"] is not None for row in recognized_rows)
    failed_count = sum(row["error"] is not None for row in recognized_rows)
    review_count = len(recognized_rows) - confirmed_count - failed_count
    reference_count = sum(is_reference(row["candidates"][0]) for row in recognized_rows)
    verified_review_count = sum(is_reviewed(row["candidates"][0])
                                and row["candidates"][0]["review"]["outcome"] == "verified"
                                for row in recognized_rows)
    empty_images = [name for name, _, count in overlays if count == 0]
    summary = (f"감지 {len(recognized_rows)}장 · 자동 확정 {confirmed_count}장 · 확인 필요 {review_count}장"
               f" · 공식 DB 조회 실패 {failed_count}장")
    if used_references:
        summary += f" · 공식 이미지 일치 {reference_count}장"
    if verified_review_count:
        summary += f" · 후보 일러스트 검증 {verified_review_count}장"
    if empty_images:
        summary += f" · 카드를 찾지 못한 사진: {', '.join(empty_images)}"
    all_confirmed = bool(recognized_rows) and confirmed_count == len(recognized_rows) and not empty_images
    return summary, all_confirmed


def draw_overlay(image, results):
    overlay = image.copy()
    draw = ImageDraw.Draw(overlay)
    line_width = max(2, image.width // 400)
    for result in results:
        color = "lime" if result["status"] == "recognized" else "red"
        points = [tuple(point) for point in result["polygon"]]
        draw.polygon(points, outline=color, width=line_width)
        draw.text(points[0], str(result["index"]), fill=color, font_size=max(16, image.width // 50))
    return overlay


st.title("유희왕 덱 사진 → 카드 목록")
st.caption("사진은 이 컴퓨터에서만 처리되며 외부로 보내지 않습니다. 네트워크는 모델 최초 다운로드와 공식 카드 DB의 "
           "카드 이름·공식 이미지(후보별 공식 일러스트 포함) 조회에만 사용됩니다. 레어도는 미리 준비한 로컬 공식 DB에서 "
           "읽으며 조회할 때 접속하지 않습니다.")

# --- 1. Model -------------------------------------------------------------
if not recognition.models_ready():
    st.warning("인식 모델(DRAW2, 약 427MB)이 아직 없습니다. 처음 한 번만 다운로드하면 data/models 에 저장됩니다.")
    if st.button("모델 다운로드 (약 427MB)", type="primary"):
        with st.status("모델 다운로드 중…", expanded=True) as status:
            try:
                recognition.download_models(progress=status.write)
            except Exception as error:
                status.update(label="모델 다운로드 실패", state="error")
                st.error(f"모델 다운로드 실패: {type(error).__name__}: {error}")
                st.stop()
            status.update(label="모델 다운로드 완료", state="complete")
        st.rerun()
    st.stop()

# --- 2. Images ------------------------------------------------------------
st.subheader("1. 사진 올리기")
uploads = st.file_uploader("덱 사진 또는 덱 편집기 스크린샷 (JPG/PNG/WebP, 파일당 최대 20MB)",
                           type=["jpg", "jpeg", "png", "webp"], accept_multiple_files=True)
if st.toggle("카메라로 찍기"):
    snapshot = st.camera_input("카메라")
    if snapshot is not None:
        uploads = [*uploads, snapshot]

images = []
for upload in uploads:
    try:
        images.append((upload.name, load_image(upload.getvalue())))
    except Exception as error:
        st.error(f"{upload.name}: 이미지를 열 수 없습니다 — {type(error).__name__}: {error}")

library_error = None
try:
    library = references.library_info()
except Exception as error:
    library = None
    library_error = f"공식 참조 이미지 라이브러리 오류: {type(error).__name__}: {error}"
if library_error:
    st.error(library_error)
elif library is None:
    st.info("공식 참조 이미지 라이브러리가 초기화되지 않았습니다. 공식 이미지 비교를 끄면 이 라이브러리 없이 "
            "DRAW2 인식(과 켜져 있으면 후보별 공식 일러스트 비교)을 사용합니다.")
else:
    st.caption(f"로컬 공식 참조 이미지: 카드 {library['card_count']}종 · 아트워크 {library['artwork_count']}장. "
               "이 라이브러리에 등록된 이미지만 비교하며, 전체 유희왕 카드를 포함하지 않습니다.")
# Always shown, so a mode the user selected is never switched off behind their back.
use_references = st.toggle("공식 이미지 비교", value=library is not None or library_error is not None,
                           key="use_references")
references_blocked = use_references and library is None
if references_blocked:
    st.warning("공식 이미지 비교가 켜져 있지만 사용할 수 있는 참조 라이브러리가 없어 인식할 수 없습니다. "
               "라이브러리를 준비하거나, 공식 이미지 비교를 직접 끄고 라이브러리 없이 인식하세요.")
use_candidate_review = st.toggle(
    "후보별 공식 일러스트 비교", value=True, key="use_candidate_review",
    help="DRAW2 점수가 낮거나 1·2순위 점수 차이가 작은 카드는 상위 3개 후보의 모든 공식 일러스트(한·일·영 페이지)를 "
         "받아 비교합니다. 위 참조 라이브러리가 없어도 동작하며, 근거가 약하면 확인 필요로 남깁니다.")

# Images, recognition modes and the reference library version all decide the recognition output.
# The candidate artwork cache is not part of it: it only grows with exact official artworks.
signature = (tuple(hashlib.sha256(upload.getvalue()).hexdigest() for upload in uploads),
             "reference" if use_references else "basic", library and library["fingerprint"],
             "candidate_review" if use_candidate_review else "no_candidate_review")
if (state.recognized_signature is not None and len(state.recognized_signature) == 3
        and state.recognized_signature == signature[:3]):
    # Rows recognized before candidate review existed keep their results and the user's edits
    # until the images or a mode actually change, or the user recognizes again.
    state.recognized_signature = signature
if signature != state.recognized_signature:
    # Images changed: recognition output from the previous images must not linger.
    state.rows = [row for row in state.rows if row["source"] == "manual"]
    state.overlays = []
    state.recognition_error = None
    state.recognized_signature = None

st.info("수량은 사진에 **보이는 카드 장수**로만 셉니다. 덱 편집기 스크린샷의 ×2/×3 배지는 읽지 않으므로 "
        "아래 목록에서 수량을 직접 확인·수정하세요. 레어도는 사진에서 판별하지 않으므로 카드마다 직접 고르세요.")

can_recognize = bool(images) and len(images) == len(uploads) and not references_blocked
if st.button("카드 인식", type="primary", disabled=not can_recognize):
    state.rows = [row for row in state.rows if row["source"] == "manual"]
    state.overlays = []
    state.recognition_error = None
    state.recognized_signature = None  # set again only when this attempt succeeds
    current_name = None
    try:
        recognizer = get_recognizer()
        new_rows = []
        with st.status("인식 중…", expanded=True) as status:
            matcher = None
            if use_references:
                status.write("공식 참조 이미지 라이브러리 불러오는 중…")
                matcher = references.ReferenceMatcher()
            reviewer = None  # prepared on the first crop that needs candidate review
            for current_name, image in images:
                status.write(f"**{current_name}**")
                results = recognizer.recognize(image, progress=status.write)
                rows = []
                for result in results:
                    candidates, result_status = result["candidates"], result["status"]
                    match = None
                    if matcher is not None:
                        status.write(f"공식 이미지 비교 {result['index']}/{len(results)}")
                        match = matcher.match(result["crop"])
                        if match is not None:
                            candidates = with_reference_first(reference_candidate(match), candidates)
                            result_status = "recognized"
                    if match is None and use_candidate_review and references.needs_candidate_review(candidates):
                        if reviewer is None:
                            reviewer = references.CandidateReviewer()
                        status.write(f"후보별 공식 일러스트 비교 {result['index']}/{len(results)}")
                        try:
                            review = reviewer.review(result["crop"], candidates)
                        except references.CandidateReviewError as error:
                            raise references.CandidateReviewError(
                                f"#{result['index']} 후보별 공식 일러스트 비교 실패: {error}") from error
                        candidates = review["candidates"]
                        # Only full automatic evidence confirms; a weak lead or no evidence stays for review.
                        result_status = "recognized" if review["accepted"] else "review"
                    rows.append({"key": uuid.uuid4().hex, "source": "recognized", "image_name": current_name,
                                 "index": result["index"], "crop": result["crop"], "candidates": candidates,
                                 "status": result_status, "choice": None, "card": None, "error": None,
                                 "quantity": 1, "name_override": ""})
                # Every row starts on its top candidate (reference match first, else highest DRAW2 score);
                # low-confidence rows keep status "review" so they still need checking.
                for row in rows:
                    if row["candidates"]:
                        if not is_reference(row["candidates"][0]):
                            status.write(f"공식 DB 조회: {row['candidates'][0]['name_en']}")
                        resolve_into(row, row["candidates"][0])
                        if row["error"]:
                            status.write(row["error"])
                overlay_results = [{**result, "status": row["status"]} for result, row in zip(results, rows)]
                state.overlays.append((current_name, draw_overlay(image, overlay_results), len(results)))
                new_rows += rows
            summary, all_confirmed = recognition_summary(new_rows, state.overlays, matcher is not None)
            status.update(label=summary, state="complete" if all_confirmed else "error")
        state.rows = new_rows + state.rows
        state.recognized_signature = signature
    except Exception as error:
        state.overlays = []
        state.recognition_error = f"인식 실패 ({current_name}): {type(error).__name__}: {error}"

if state.recognition_error:
    st.error(state.recognition_error)
if state.recognized_signature is not None:
    # Counted from the current rows, so a successful retry or a new selection updates the summary.
    summary, all_confirmed = recognition_summary([row for row in state.rows if row["source"] == "recognized"],
                                                 state.overlays, state.recognized_signature[1] == "reference")
    if all_confirmed:
        st.success(summary + " — 그래도 아래에서 이름과 수량을 확인하세요.")
    else:
        st.warning(summary + " — 아래 목록에서 확인 필요·실패 항목을 처리하세요.")
for name, overlay, count in state.overlays:
    with st.expander(f"{name}: 카드 {count}장 감지 (초록=인식, 빨강=확인 필요)", expanded=count == 0):
        if count == 0:
            st.warning("이 사진에서 카드를 찾지 못했습니다. 더 밝고 정면에서 찍은 사진을 사용하거나 카드를 직접 추가하세요.")
        st.image(overlay, width="stretch")

# --- 3. Review ------------------------------------------------------------
st.subheader("2. 카드 확인·수정")
language = st.radio("내보낼 카드명 언어", list(LANGUAGES), horizontal=True)
language_key = LANGUAGES[language]

with st.form("add_card", clear_on_submit=True):
    st.write("카드 직접 추가 (영어 정식 카드명으로 공식 DB를 조회합니다)")
    add_columns = st.columns([4, 1, 1])
    manual_name = add_columns[0].text_input("영어 카드명", placeholder="예: Ash Blossom & Joyous Spring")
    manual_quantity = add_columns[1].number_input("수량", min_value=1, step=1, value=1)
    if add_columns[2].form_submit_button("추가") and manual_name.strip():
        try:
            card = catalog.resolve_card(manual_name.strip())
        except Exception as error:
            st.error(f"'{manual_name}' 조회 실패: {type(error).__name__}: {error}")
        else:
            state.rows.append({"key": uuid.uuid4().hex, "source": "manual", "image_name": None, "index": None,
                               "crop": None, "candidates": [], "status": "manual", "choice": None,
                               "card": card, "error": None, "quantity": int(manual_quantity),
                               "name_override": ""})

if state.rows:
    st.caption("카드 자체가 틀렸다면 표시 이름을 고치지 말고, 인식 후보에서 다시 고르거나 삭제한 뒤 "
               "정확한 영어 공식 카드명으로 직접 추가하세요. 표시 이름은 내보내는 '카드명' 열만 바꿉니다.")
else:
    st.write("아직 카드가 없습니다. 사진을 인식하거나 카드를 직접 추가하세요.")

for row in list(state.rows):
    key = row["key"]
    with st.container(border=True):
        image_column, reference_column, edit_column = st.columns([1, 1, 4])
        if row["crop"] is not None:
            image_column.image(row["crop"], caption=f"{row['image_name']} #{row['index']}")
        else:
            image_column.caption("직접 추가한 카드")
        chosen = next((c for c in row["candidates"] if c["card_id"] == row["choice"]), None)
        if row["card"] and row["card"]["image_path"]:
            reference_caption = "공식 참조 이미지"
            if chosen is not None and is_reviewed(chosen):
                reference_caption = f"비교한 공식 일러스트 (ciid {chosen['review']['ciid']})"
            reference_column.image(row["card"]["image_path"], caption=reference_caption)

        if row["candidates"]:
            with st.spinner("후보 공식 이름 조회 중…"):
                labels = ["— 선택하세요 —"] + [candidate_label(c) for c in row["candidates"]]
            failed_names = {c["name_en"]: lookup(c["name_en"])["error"] for c in row["candidates"]
                            if not is_reference(c) and not is_reviewed(c) and "error" in lookup(c["name_en"])}
            if failed_names:
                edit_column.warning("후보 공식 이름 조회 실패 — 이름을 확인할 수 없습니다:\n\n"
                                    + "\n\n".join(failed_names.values()))
                if edit_column.button("공식 DB 다시 조회", key=f"retry_{key}"):
                    for failed_key in {catalog.normalize_name(name_en) for name_en in failed_names}:
                        del state.card_lookups[failed_key]
                    # Every row that already selected a candidate takes the refreshed shared result;
                    # unselected rows stay unselected.
                    for other_row in state.rows:
                        if other_row["choice"] is not None and other_row["candidates"]:
                            resolve_into(other_row, next(c for c in other_row["candidates"]
                                                         if c["card_id"] == other_row["choice"]))
                    st.rerun()
            ids = [None] + [c["card_id"] for c in row["candidates"]]
            choice_key = f"choice_{key}"
            # The browser keeps showing the label it last received for the selected index, so when the
            # labels change (e.g. a lookup recovered) the row's choice is written again to resend it.
            if row.get("candidate_labels") != labels:
                state[choice_key] = ids.index(row["choice"])
                row["candidate_labels"] = labels
            selected = edit_column.selectbox(
                "인식 후보" + (" — 신뢰도 낮음, 확인 필요" if row["status"] == "review" else ""),
                range(len(labels)), format_func=labels.__getitem__, key=choice_key)
            if ids[selected] != row["choice"]:
                if ids[selected] is None:
                    row.update(choice=None, card=None, error=None)
                else:
                    with st.spinner("공식 DB 조회 중…"):
                        resolve_into(row, row["candidates"][selected - 1])
                st.rerun()

        if row["card"]:
            card = row["card"]
            unavailable = "(공식 이름 없음)"
            if chosen is not None and is_reference(chosen):
                origin = (f" · 공식 이미지 일치 (특징점 {chosen['inliers']}개, 일치 비율 {chosen['inlier_ratio']:.2f},"
                          f" 분포 {chosen['spread']:.2f}, 차이 {chosen['margin']:.2f})")
            elif chosen is not None and is_reviewed(chosen):
                review = chosen["review"]
                verdict = {"verified": "일러스트 일치 검증됨",
                           "leading": f"일러스트 근거 약함 — 자동 확정 기준(특징점 {references.MIN_INLIERS}개) 미달, 확인 필요",
                           "none": "일러스트 근거 없음 — 모델 후보, 확인 필요"}[review["outcome"]]
                origin = (f" · DRAW2 ID {row['choice']} · 모델 점수 {chosen['score']:.2%}"
                          f" · 후보 일러스트 비교: 특징점 {review['inliers']}개, 일치 비율 {review['inlier_ratio']:.2f},"
                          f" 분포 {review['spread']:.2f} · {verdict}")
            else:
                origin = f" · DRAW2 ID {row['choice']}" if row["choice"] else ""
            edit_column.caption(f"한국어: {card['name_ko'] or unavailable} · 일본어: {card['name_ja'] or unavailable}"
                                f" · 영어: {card['name_en'] or unavailable} · 공식 CID {card['cid']}" + origin)

            # The rarity belongs to one CID: a new or changed card starts on its lowest official rarity
            # (rows from before this feature have no rarity yet and start there too). Otherwise the
            # user's choice is kept, and a choice the official list no longer has is never replaced.
            result = rarity_lookup(card["cid"])
            rarity_key = f"rarity_{key}"
            if row.get("rarity_cid") != card["cid"]:
                row["rarity"] = None
                row["rarity_cid"] = None
            if "error" in result:
                if edit_column.button("레어도 다시 조회", key=f"retry_rarity_{key}"):
                    del state.rarity_lookups[card["cid"]]
                    st.rerun()
            elif result["rarities"]:
                options = result["rarities"]
                if row["rarity_cid"] is None:
                    row["rarity"] = options[0]
                    row["rarity_cid"] = card["cid"]
                    row["rarity_options"] = None  # forces the widget to show the new default
                # Write the widget only when its options or the row's rarity were reset, so reruns keep the
                # user's selection; an invalid stored rarity leaves the widget empty instead of picking one.
                if rarity_key not in state or row.get("rarity_options") != options:
                    state[rarity_key] = row["rarity"] if row["rarity"] in options else None
                    row["rarity_options"] = options
                selected = edit_column.selectbox("레어도", options, format_func=rarities.RARITY_LABELS.__getitem__,
                                                 key=rarity_key, placeholder="레어도를 선택하세요")
                if selected is not None and selected != row["rarity"]:
                    row["rarity"] = selected

        name_column, quantity_column, delete_column = edit_column.columns([4, 1, 1])
        row["name_override"] = name_column.text_input(
            "내보낼 표시 이름 (비우면 공식 이름)", value=row["name_override"], key=f"name_{key}",
            placeholder=export_name({**row, "name_override": ""}, language_key) or "공식 이름 없음 — 직접 입력",
            help="엑셀/CSV의 '카드명' 열에만 쓰입니다. 카드 식별(공식 CID·공식 이름 열)은 바뀌지 않습니다. "
                 "카드 자체가 틀렸다면 인식 후보에서 다시 고르거나, 삭제한 뒤 정확한 영어 공식 카드명으로 직접 추가하세요.")
        row["quantity"] = int(quantity_column.number_input("수량", min_value=1, step=1, value=row["quantity"],
                                                           key=f"quantity_{key}"))
        if delete_column.button("삭제", key=f"delete_{key}"):
            state.rows.remove(row)
            st.rerun()

        problem = problems_of(row, language_key)
        if problem:
            edit_column.warning(problem)

# --- 4. Export ------------------------------------------------------------
st.subheader("3. 내보내기")
st.caption("기본 열은 카드명·레어도·수량이며, 고른 열이 그 뒤에 붙습니다. 카드는 메인 덱 몬스터 → 마법 → 함정 → "
           "엑스트라 덱 몬스터 순으로 정렬되고, 같은 카드의 레어도는 낮은 것부터 이어집니다.")
EXPORT_OPTION_LABELS = {"name_ko": "한국어 카드명", "name_ja": "일본어 카드명", "name_en": "영어 카드명", "cid": "CID"}
optional_fields = []
for column, (field, label) in zip(st.columns(len(EXPORT_OPTION_LABELS)), EXPORT_OPTION_LABELS.items()):
    if column.checkbox(label, key=f"export_{field}"):
        optional_fields.append(field)
optional_fields = tuple(optional_fields)
unresolved = [row for row in state.rows if problems_of(row, language_key)]
cards = []
export_error = None
if state.rows and not unresolved:
    entries = [{"cid": row["card"]["cid"], "rarity": row["rarity"], "name": export_name(row, language_key),
                "name_ko": row["card"]["name_ko"], "name_ja": row["card"]["name_ja"],
                "name_en": row["card"]["name_en"], "quantity": row["quantity"]} for row in state.rows]
    try:
        # Validation, aggregation and deck order all succeed or nothing is shown or downloadable.
        cards = exports.sort_cards(exports.aggregate(entries))
    except ValueError as error:
        export_error = str(error)

if unresolved:
    st.warning(f"확인이 필요한 카드 {len(unresolved)}장이 남아 있습니다. 후보를 선택·수정하거나 삭제하세요.")
if export_error:
    st.error(export_error)
if cards:
    st.dataframe([dict(zip(exports.headers(optional_fields), exports.row_values(card, optional_fields)))
                  for card in cards], hide_index=True)
    st.write(f"총 {sum(card['quantity'] for card in cards)}장, {len({card['cid'] for card in cards})}종"
             f" (카드·레어도별 {len(cards)}줄)")

# Any change to what would be exported (images, card identity, rarity, language, names, quantities,
# added or deleted rows, selected optional columns, the prepared lines and their deck order, including a
# failed preparation) clears the confirmation, so the user must confirm the final list again.
# signature[:3]: the candidate review mode only matters through the recognized rows it produced.
export_fingerprint = (signature[:3], language_key, optional_fields, tuple(
    (row["key"], row["choice"], row["card"] and row["card"]["cid"], row.get("rarity"), export_name(row, language_key),
     row["quantity"])
    for row in state.rows), tuple((card["cid"], card["rarity"]) for card in cards))
if export_fingerprint != state.export_fingerprint:
    state.confirmed = False
    state.export_fingerprint = export_fingerprint
confirmed = st.checkbox("모든 카드의 이름·수량·레어도를 직접 확인했습니다", key="confirmed", disabled=not cards)
ready = bool(cards) and confirmed
download_columns = st.columns(2)
download_columns[0].download_button(
    "Excel(XLSX) 다운로드", exports.to_xlsx_bytes(cards, optional_fields) if ready else b"", file_name="deck.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", disabled=not ready)
download_columns[1].download_button(
    "CSV 다운로드", exports.to_csv_bytes(cards, optional_fields) if ready else b"", file_name="deck.csv",
    mime="text/csv", disabled=not ready)
