"""Rule-based version parsing (ingest_version.py) - the cases below mirror
the real Google Drive file names in the dev database plus the marker
vocabulary the module documents; each row is (name, label, number,
is_final, family_key)."""

import pytest

from knowledge_forge.ingest_version import parse_version, version_sort_key

CASES = [
    # --- v-style ---
    ("보고서_v1.docx", "v1", 1.0, False, "보고서"),
    ("보고서_V2.1.docx", "V2.1", 2.1, False, "보고서"),
    ("seah_execution_plan_v3_1.html", "v3_1", 3.1, False, "seah execution plan"),
    ("Equipment_Cata_ver12.0.xlsx", "ver12.0", 12.0, False, "equipment cata"),
    ("Equipment_Cata_ver13.0.xlsx", "ver13.0", 13.0, False, "equipment cata"),
    ("AEGIS_방법론_v1.1_현장가이드.html", "v1.1", 1.1, False, "aegis 방법론 현장가이드"),
    ("계약_통합관리_v6_2.xlsx", "v6_2", 6.2, False, "계약 통합관리"),
    ("kv_ouput_v1.xlsx", "v1", 1.0, False, "kv ouput"),
    ("Design version 3.pdf", "version 3", 3.0, False, "design"),
    # --- rev / r ---
    ("1. SeAH_Keyword 정의_포항제품개발팀 r1.xlsx", "r1", 1.0, False, "1 seah keyword 정의 포항제품개발팀"),
    ("spec_rev2.docx", "rev2", 2.0, False, "spec"),
    ("(공유용) 애자일소다_Utility Header 2D Routing_R1_260729.pptx", "R1", 1.0, False,
     "공유용 애자일소다 utility header 2d routing"),
    # --- N차 ---
    ("제안서 3차 수정본.hwp", "3차 수정본", 3.0, False, "제안서"),
    ("제안서_1차.hwp", "1차", 1.0, False, "제안서"),
    ("제안서_2차 개정.hwp", "2차 개정", 2.0, False, "제안서"),
    # "3차" followed by other words is an ordinal, not a revision
    ("3차 인터뷰를 위한 회의자료 송부", None, None, False, "3차 인터뷰를 위한 회의자료 송부"),
    # --- (N) copy suffix ---
    ("LICO_FNC_control_endpoint_코더_디버깅_가이드 (1).md", "(1)", 1.0, False,
     "lico fnc control endpoint 코더 디버깅 가이드"),
    ("LICO_FNC_control_endpoint_코더_디버깅_가이드.md", None, None, False,
     "lico fnc control endpoint 코더 디버깅 가이드"),
    ("포항공장 생산 CAPA CHART(2021)_PM53 업데이트.xlsx", None, None, False,
     "포항공장 생산 capa chart 2021 pm53 업데이트"),
    # --- final / draft / revised ---
    ("plot_plan_demo_final.mp4", "final", None, True, "plot plan demo"),
    ("계획서_최종.pptx", "최종", None, True, "계획서"),
    ("계획서_최종본.pptx", "최종본", None, True, "계획서"),
    ("계획서_FINAL_수정.pptx", "FINAL_수정", None, True, "계획서"),
    ("계획서_v3_최종.pptx", "v3_최종", 3.0, True, "계획서"),
    ("세아제강_제조가부_자동검토_에이전트_PRD_초안.html", "초안", 0.0, False, "세아제강 제조가부 자동검토 에이전트 prd"),
    ("proposal_draft.docx", "draft", 0.0, False, "proposal"),
    ("매출계약서List(원본).xlsx", "원본", 0.0, False, "매출계약서list"),
    ("계획서_수정본.pptx", "수정본", None, False, "계획서"),
    ("계획서_v2_수정본.pptx", "v2_수정본", 2.0, False, "계획서"),
    # --- date stamps ---
    ("PlotPlan_서버_구동방법_20260728.pptx", "20260728", None, False, "plotplan 서버 구동방법"),
    ("SeAH_착수보고서_260820.docx", "20260820", None, False, "seah 착수보고서"),
    ("API 5L MPS and ITP - HFW_X70M_ETC_2026.07.30.xlsx", "20260730", None, False, "api 5l mps and itp hfw x70m etc"),
    ("TalkFile_프로젝트_인력관리_양식_26.07.19.xlsx", "20260719", None, False, "talkfile 프로젝트 인력관리 양식"),
    # a capture timestamp (date + time) is not a version stamp
    ("20260729_183948.jpg", None, None, False, "20260729 183948"),
    ("스크린샷 2026-07-31 오후 5.57.35.png", None, None, False, "스크린샷 2026 07 31 오후 5 57 35"),
    ("IMG_20260824_230138.jpg", None, None, False, "img 20260824 230138"),
    # date after the marker still counts (marker nearest the end wins)
    ("3D TF_DTSP_Plot Simulation_V2 패치 검토_260824_R1.pptx", "R1", 1.0, False,
     "3d tf dtsp plot simulation 패치 검토"),
    # v<date>
    ("삼성_E&A_26_Plot_Plan_9월이후_배포계획_통합WBS_v260831.xlsx", "v260831", None, False,
     "삼성 e a 26 plot plan 9월이후 배포계획 통합wbs"),
    # --- mail subjects ---
    ("Re: [세아제강] 제조가부 샘플 자료 송부 건", None, None, False, "세아제강 제조가부 샘플 자료 송부 건"),
    ("Fwd: 주간보고 v2", "v2", 2.0, False, "주간보고"),
    # --- no marker / noise ---
    ("image.png", None, None, False, "image"),
    ("container-cve-scan-report.md", None, None, False, "container cve scan report"),
    ("애자일소다_삼성E&A 26' Plot Plan 프로젝트_주간보고_W13_backend.docx", None, None, False,
     "애자일소다 삼성e a 26 plot plan 프로젝트 주간보고 w13 backend"),
    ("SMILE-ED-00-PIP-SPC-000-0005_Marking_for_Piping_Material_B.xlsx", None, None, False,
     "smile ed 00 pip spc 000 0005 marking for piping material b"),
]


@pytest.mark.parametrize(("name", "label", "number", "is_final", "family_key"), CASES)
def test_parse_version(name, label, number, is_final, family_key):
    info = parse_version(name)
    assert info is not None
    assert (info.label, info.number, info.is_final, info.family_key) == (label, number, is_final, family_key)


@pytest.mark.parametrize(
    "names",
    [
        ("보고서_v1.docx", "보고서_v2.docx", "보고서_최종.docx", "보고서.docx", "보고서 (1).docx", "Re: 보고서"),
        ("Equipment_Cata_ver12.0.xlsx", "Equipment_Cata_ver13.0.xlsx", "equipment-cata.XLSX"),
        ("제안서 3차 수정본.hwp", "제안서_1차.hwp", "제안서_초안.hwp", "제안서.hwp"),
        ("SeAH_착수보고서_260820.docx", "SeAH_착수보고서_260901.docx", "seah 착수보고서.docx"),
    ],
)
def test_family_key_is_shared_across_versions(names):
    keys = {parse_version(n).family_key for n in names}
    assert len(keys) == 1, keys


def test_family_key_separates_derivatives():
    a = parse_version("routing_sample.xlsx").family_key
    b = parse_version("routing_sample_삼성회신.xlsx").family_key
    assert a != b


@pytest.mark.parametrize("name", ["", "   ", None])
def test_empty_name_returns_none(name):
    assert parse_version(name) is None


def test_date_stamp_recorded():
    assert parse_version("SeAH_착수보고서_260820.docx").date_stamp == "20260820"
    assert parse_version("통합WBS_v260831.xlsx").date_stamp == "20260831"
    assert parse_version("보고서_v2.docx").date_stamp is None


def test_version_sort_key_orders_draft_unmarked_numbered_revised_final():
    names = [
        "보고서_최종.docx",
        "보고서_v2_수정본.docx",
        "보고서_v3.docx",
        "보고서.docx",
        "보고서_초안.docx",
        "보고서_v2.docx",
        "보고서_v1.docx",
        "보고서_v3_최종.docx",
        "보고서_수정본.docx",
    ]
    ordered = sorted(names, key=lambda n: _sort_key(n))
    assert ordered == [
        "보고서_초안.docx",
        "보고서.docx",
        "보고서_v1.docx",
        "보고서_v2.docx",
        "보고서_v2_수정본.docx",
        "보고서_v3.docx",
        "보고서_수정본.docx",
        "보고서_최종.docx",
        "보고서_v3_최종.docx",
    ]


def test_version_sort_key_orders_dates():
    names = ["보고서_260901.docx", "보고서_260820.docx", "보고서.docx"]
    assert sorted(names, key=_sort_key) == ["보고서.docx", "보고서_260820.docx", "보고서_260901.docx"]


def _sort_key(name):
    info = parse_version(name)
    # the store re-derives the key from the persisted metadata only
    # (label/number/is_final), so the test does the same
    return version_sort_key(info.label, info.number, info.is_final)
