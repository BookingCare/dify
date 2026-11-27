"""Catalog API for the BookingCare package chatbot.

Reads the editable data folder (DATA_DIR, default ../du_lieu) and answers lookups by id, so the chatbot
never depends on search ranking. Data is reloaded automatically whenever a file in DATA_DIR changes.
Standard library only.

Endpoints
  GET  /health          counts + number of data errors
  GET  /danh_sach       short lists of facilities and packages (text, injected into the "needs" prompt)
  POST /tra_cuu         body = the needs JSON (raw text from the LLM) -> full context text for the answer;
                        optional first line "TRUOC: <ids>" and last line "HOI: <customer question>"
  GET  /bao_cao         data quality report (errors / warnings per file and row)
  GET  /goi/<ma>        one package as the chatbot sees it (for checking)
  GET  /co_so/<ma>      one facility as the chatbot sees it
  GET  /openapi.json    OpenAPI schema of the endpoints the chatbot uses (Dify custom tool)
"""
import collections
import csv
import json
import os
import random
import re
import threading
import unicodedata
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(ROOT, "du_lieu"))
PORT = int(os.environ.get("PORT", "8000"))

LOG_DIR = os.environ.get("LOG_DIR", os.path.join(ROOT, "nhat_ky"))

# The 6 document types (loại giấy) of US-03, plus the other purposes.
LOAI_GIAY_TEN = {"lai_xe": "lái xe", "di_lam_di_hoc": "đi làm – đi học", "work_permit": "Work Permit",
                 "visa": "visa", "du_hoc": "du học", "di_nhat": "đi Nhật (JPETS, xuất khẩu lao động)",
                 "ket_hon_nuoc_ngoai": "đăng ký kết hôn có yếu tố nước ngoài"}
LOAI_GIAY = set(LOAI_GIAY_TEN)
# words (without diacritics) that name a document type in the customer's own question
LOAI_GIAY_TU = {"lai_xe": ["lai xe", "bang lai"], "di_lam_di_hoc": ["di lam", "xin viec", "di hoc", "nhap hoc"],
                "work_permit": ["work permit", "giay phep lao dong"], "visa": ["visa", "dinh cu"],
                "du_hoc": ["du hoc"], "di_nhat": ["di nhat", "jpets", "xuat khau lao dong", "xkld"]}
GIAY_TO_CHUA_RO = "giay_to"
CHUA_RO_DOI_TUONG = "chua_ro_doi_tuong"  # "khám định kỳ", "khám cho nhóm 10 người": for oneself or for a company?
# a question about Thông tư 25 (or its Mẫu số 03): the checklist from du_lieu/danh_muc_tt25.md is added
# a check-up "theo Thông tư 25 / 32 / 14" with nothing personal in the question is the company check-up (US-01)
TT_DOANH_NGHIEP = re.compile(r"\b(tt|thong tu)\s*(so\s*)?(32\s*/\s*)?(25|32|14)\b")
# a company question about e-invoices (VAT) or pushing results to the health data system: a point for the facility that has it
# the customer is married: packages for singles / before marriage do not fit (unless asked for by name or purpose)
DA_KET_HON = re.compile(r"\b(da ket hon|da lap gia dinh|(?<!chua )co gia dinh|da co gia dinh|(?<!chua )co chong|(?<!chua )co vo|da cuoi|vo chong)\b")
CHO_DOC_THAN = re.compile(r"\b(doc than|chua ket hon|chua lap gia dinh|chua co gia dinh)\b")


def gioi_han_tuoi(sections):
    """The age range a package states in its introduction ("dưới 30 tuổi", "từ 18 tuổi", "trên 40 tuổi"): (min, max)."""
    t = norm(" ".join(txt for h, txt in sections or [] if "gioi thieu" in norm(h)))
    lo, hi = 0, 200
    for a, b in re.findall(r"\b(\d{1,2})\s*(?:-|den)\s*(\d{1,2})\s*tuoi\b", t):
        lo, hi = max(lo, int(a)), min(hi, int(b))
    for n in re.findall(r"\bduoi\s*(\d{1,2})\s*tuoi\b", t):
        hi = min(hi, int(n) - 1)
    for n in re.findall(r"\b(?:tu|tren)\s*(\d{1,2})\s*tuoi\b", t):
        lo = max(lo, int(n))
    return lo, hi


# marriage registration with a foreigner: a certificate check-up, not a pre-marriage package
KET_HON_NUOC_NGOAI = re.compile(r"(ket hon|dang ky ket hon).{0,40}(nguoi viet nam|nguoi nuoc ngoai|yeu to nuoc ngoai)|"
                                r"(nguoi nuoc ngoai|yeu to nuoc ngoai).{0,60}ket hon")
# "người lớn", "trung niên": an adult older than the "under 30" packages
NGUOI_LON = re.compile(r"\b(nguoi lon|trung nien|lon tuoi|cao tuoi|nguoi gia|nguoi cao tuoi)\b")
DN_HOI_HOA_DON = re.compile(r"\b(hoa don|vat|xuat hoa don)\b")
# the customer asks about price: only then does a known (lower) price count in the company facility choice
DN_HOI_GIA = re.compile(r"\b(bao gia|bang gia|muc gia|gia (bao nhieu|ca|re|tham khao|kham|goi|the nao|nhu nao)|chi phi|"
                        r"bao nhieu tien|het bao nhieu|re nhat|re hon|tiet kiem|ngan sach|kinh phi)\b|\bgia\s*$")
DN_HOI_DAY_THONG_TIN = re.compile(r"\b(day thong tin|co so du lieu|lien thong|so suc khoe dien tu|cap nhat ket qua)\b")
TT_CA_NHAN = re.compile(r"\b(xin viec|di lam|di hoc|nhap hoc|lai xe|bang lai|ca nhan|ban than|nop cho|ho so|giay kham)\b")
# one person's certificate handed to their company ("khám định kỳ nộp cho công ty", "lái xe cho công ty"):
# a personal check-up, not the company flow, unless the question is about staff / several people
CA_NHAN_CHO_CTY = re.compile(r"\b(nop (cho |ve |len )?(cong ty|cty|cho lam|co quan)|(lai xe|di lam|xin viec) cho (cong ty|cty))\b")
DN_NHIEU_NGUOI = re.compile(r"\b(nhan vien|nguoi lao dong|lao dong|cbcnv|toan the|\d+ nguoi|doanh nghiep)\b")
# situations that asked the customer something: the next turn suggests instead of asking the same again
DA_HOI_LAI = {"ca_nhan_hoi_khu_vuc", "goi_goi_y", "hoi_doi_tuong", "hoi_nhu_cau", "giay_hoi_khu_vuc", "co_so_chua_hop_tac_hoi_khu_vuc"}
# the customer asks how to reach someone (number, Zalo, a person) or says booking does not work
HOI_LIEN_HE = re.compile(r"\b(so dien thoai|sdt|hotline|tong dai|zalo|(?<!co )so nao|goi (cho )?ai|lien he (the nao|nhu nao|bang cach nao|voi)|"
                         r"nguoi (that|truc)|nhan vien tu van|tu van vien|khong co nguoi|(bam|dat|dang ky|goi)[\w .\-]{0,25}khong duoc|bi loi)\b")
# the customer asks about health insurance (BHYT), not a commercial insurer: the fixed BHYT sentence is added
HOI_BHYT = re.compile(r"\bbhyt\b|bao hiem y te|the bao hiem|bao hiem xa hoi|\bbhxh\b|"
                      r"(?<!bao lanh )\bbao hiem\b(?! (thuong mai|nhan tho|bao viet|pvi|bao minh|liberty|manulife|"
                      r"prudential|aia|fwd|bao lanh|tu nhan|cong ty))")
CAU_BHYT = ("Theo Luật Bảo hiểm y tế, khám sức khỏe (khám tổng quát, khám định kỳ, khám theo gói hay khám để làm "
            "giấy tờ) không thuộc phạm vi BHYT chi trả, nên chi phí khám do người khám tự trả. BHYT chỉ áp dụng khi "
            "đi khám vì có bệnh, đúng thủ tục, tại cơ sở có nhận BHYT.")
HOI_TT25 = re.compile(r"\b(tt|thông tư|thong tu)\s*(số\s*)?(32\s*/\s*)?25\b|mẫu\s*(số\s*)?0?3\b|phụ lục\s*(số\s*)?xxiv", re.I)
# "TT32/25", "Thông tư 25", "Mẫu số 03" name a regulation, not a package: the bot answers those from its
# regulation documents, so they must not come back as "not found in the data".
VAN_BAN = re.compile(r"^\W*(tt|thông tư|thong tu|cv|công văn|cong van|nđ|nghị định|qđ|quyết định|mẫu|mau|"
                     r"phụ lục|phu luc)\W*(số\s*)?(\d|[IVX]+\b)", re.I)
# Links shown to book / leave contact details (lead). The package's own link (goi/goi.csv `link`) is only its
# detail page. Business packages collect leads on the corporate page instead.
# Every bookingcare.vn link the chatbot gives out carries utm_source, so BookingCare can track visits and leads that
# come from the chatbot (added to the whole response in gan_utm, data links included).
UTM_SOURCE = os.environ.get("UTM_SOURCE", "AI_goikham")
LINK_BOOKINGCARE = re.compile(r"https?://(?:www\.)?bookingcare\.vn(?:/[^\s\"'<>()\[\]]*)?")
LEAD_LINK = "https://bookingcare.vn/tu-van-nhanh?entityName=clinic&entityId={ma}"
LEAD_LINK_DOANH_NGHIEP = "https://bookingcare.vn/suc-khoe-doanh-nghiep/dang-ky"
# the general lead form (no facility or package): for a need the data has no package for
LEAD_LINK_TU_VAN = "https://bookingcare.vn/tu-van-nhanh"
HOTLINE_BOOKINGCARE = "024-7301-2468"
ZALO_BOOKINGCARE = "https://zalo.me/3344993609824066411"
# Dify's chat renders this markup as a button that opens the link (MarkdownButton)
CTA_DOANH_NGHIEP = f'<button data-variant="primary" data-link="{LEAD_LINK_DOANH_NGHIEP}">Đăng ký tư vấn khám doanh nghiệp</button>'
CTA_ZALO = f'<button data-variant="secondary" data-link="{ZALO_BOOKINGCARE}">Chat Zalo với BookingCare</button>'
CTA_TU_VAN = f'<button data-variant="primary" data-link="{LEAD_LINK_TU_VAN}">Để lại thông tin tư vấn</button>'
# the contact block that ends every company answer: the sign-up form, Zalo and the BookingCare hotline
DAN_LIEN_HE = "Bạn để lại thông tin để đội tư vấn BookingCare liên hệ và gửi báo giá, hoặc chat Zalo để trao đổi ngay:"
KHOI_LIEN_HE = f"{DAN_LIEN_HE}\n\n{CTA_DOANH_NGHIEP} {CTA_ZALO}\n\nHoặc gọi BookingCare: **{HOTLINE_BOOKINGCARE}**"
# a personal / document need the data has no package for: instead of turning the customer away, ask them to leave
# their details on the lead form so BookingCare's advisers can suggest a package (lead), with these lines copied
# verbatim. The chatbot itself keeps no contact details, so it never says the need has been recorded.
DAN_LIEN_HE_CA_NHAN = "Bạn có thể để lại thông tin để đội ngũ BookingCare hỗ trợ, tư vấn thêm về gói khám phù hợp:"
KHOI_LIEN_HE_CA_NHAN = f"{DAN_LIEN_HE_CA_NHAN}\n\n{CTA_TU_VAN} {CTA_ZALO}\n\nHoặc gọi BookingCare: **{HOTLINE_BOOKINGCARE}**"
# The instructions the answer step gets in each situation ("GHI CHÚ: ..."): loi_dan.md next to this file.
LOI_DAN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "loi_dan.md")
LOI_DAN_GIA_TRI = {"khoi_lien_he_ca_nhan": KHOI_LIEN_HE_CA_NHAN}  # values loi_dan.md may insert
MAX_CO_SO_DN = 3
# BookingCare (2026-11-03): every partner facility pushes the check-up information to the system itself
DN_DAY_THONG_TIN = ("các cơ sở y tế có gói khám doanh nghiệp (các cơ sở dưới đây) hỗ trợ tự đẩy thông tin khám sức "
                    "khỏe lên hệ thống theo Thông tư 25/2026/TT-BYT.")
# a company question beyond "we want check-ups" (price, checklist, sign-up...): the sign-up button stays even
# while the area is still unknown (US-01: a price question always ends with the sign-up link)
DN_ASKS_MORE = re.compile(r"\b(gia|chi phi|bao gia|bao nhieu tien|danh muc|hang muc|kham (nhung )?(gi|nao)|thong tu|tt ?25|"
                          r"quy trinh|dang ky|tu van|lien he|hop dong|hoa don|vat|so dien thoai|hotline|zalo|goi lai|"
                          # questions the advisers answer (US-01: hand over to the contact channels)
                          r"toi thieu|bao nhieu nguoi|\d+ nguoi|so luong|tai (van phong|cong ty|cho|nha may|xuong)|"
                          r"tra ket qua|bao lau|mat bao lau|thoi gian|bhyt|bao hiem)\b")
# a company question about what a facility's company check-up includes or how it runs: its own details
# (kien_thuc/doanh_nghiep/co_so/<ma_co_so>.md) are added, as they are when the customer names the facility
DN_CHI_TIET = re.compile(r"danh muc|hang muc|kham (nhung )?(gi|nao)|gom (nhung )?gi|bao gom|xet nghiem|lay mau|uu dai|"
                         r"quyen loi|hoa don|vat|hop dong|gio kham|thoi gian kham|nhin an|tai (cong ty|van phong)|luu y")
# public hospitals (co_so/kham_tong_quat.csv): BookingCare does not book there, the bot only explains how
# to get a general check-up; (column, label, required)
KTQ_FIELDS = [("hinh_thuc_kham", "Hình thức khám tổng quát", True), ("noi_dang_ky", "Nơi đăng ký", True),
              ("cach_dat_lich", "Cách đặt lịch", True), ("thoi_gian_kham", "Thời gian khám", True),
              ("chi_phi", "Chi phí tham khảo", False), ("bhyt", "BHYT cho khám tổng quát", True),
              ("luu_y", "Lưu ý đi khám", False)]
HINH_THUC_KHAM = {"tai_co_so": "khám tại cơ sở", "tai_doanh_nghiep": "khám tại doanh nghiệp",
                  "ca_hai": "khám tại cơ sở hoặc tại doanh nghiệp"}
# facility content about walk-in visits (prices, booking a visit): the bot only advises on packages
KHAM_LE_SECTION = re.compile(r"^(chi phí|giá|bảng giá|cách thức đặt lịch)", re.I)
# a general question about getting ready for a check-up ("trước khi xét nghiệm có nhịn đói không"): the shared
# answers from du_lieu/kien_thuc/hoi_dap/ are added
HOI_CHUAN_BI = re.compile(r"\bnhin\b|an sang|uong (nuoc|gi|thuoc|ca phe)|dang (uong|dung) thuoc|thuoc (huyet ap|tieu duong)|"
                          r"mang theo|chuan bi|kinh nguyet|co thai|mang thai|ca phe|hut thuoc|ruou bia")
# "BV Đại học Y cơ sở Hoàng Mai": a branch named after the hospital, matched by the part before it
BRANCH = re.compile(r"\s+(cơ sở|co so|cs|chi nhánh|chi nhanh)\b.*$", re.I)
# the customer asks for a walk-in / single-specialty visit instead of a package
HOI_KHAM_LE = re.compile(r"kham (le|rieng)\b|kham (theo )?chuyen khoa|(gia|chi phi|bang gia) kham (le|rieng|chuyen khoa|"
                         r"mat|nhi|tai mui hong|phu khoa|san|noi khoa|da lieu|rang|tim)|dat lich kham (tai|o) (co so|benh vien|"
                         r"phong kham)")
MUC_DICH = {"tong_quat", "ung_thu", "tien_hon_nhan", "tre_em", "doanh_nghiep", "chuyen_khoa"} | LOAI_GIAY
# Packages for these purposes are only suggested when the customer asks for that purpose.
SPECIAL = {"tien_hon_nhan", "tre_em", "doanh_nghiep"} | LOAI_GIAY
THU = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]
MAX_GOI, MAX_CO_SO = 5, 3
MAX_LIST = 5  # packages shown when listing everything at a facility (the most suitable first)
GOI_PHAN = {"gioi_thieu": "Giới thiệu", "danh_muc": "Danh mục khám", "chuan_bi": "Chuẩn bị trước khi khám",
            "luu_y_giay": "Lưu ý giấy tờ"}  # the last one only for document-type packages


# kham_tong_quat.csv rows written by he_thong/scripts/nhap_bv_cong.py (nguon column)
NGUON_NHAP_BV_CONG = "Danh sách bệnh viện công"
# the kind of check-up asked at a public hospital, as the note names it
LOAI_KHAM_TEN = {"tong_quat": "khám tổng quát", "ung_thu": "tầm soát ung thư", "tien_hon_nhan": "khám tiền hôn nhân",
                 "tre_em": "khám cho trẻ em", "chuyen_khoa": "khám chuyên khoa"}


def loai_kham(purpose, question):
    if purpose in LOAI_GIAY:
        return f"khám sức khỏe lấy giấy {LOAI_GIAY_TEN[purpose]}"
    if purpose in LOAI_KHAM_TEN:
        return LOAI_KHAM_TEN[purpose]
    if HOI_KHAM_LE.search(norm(question)):
        return "khám chuyên khoa / khám lẻ"
    return "nhu cầu khám khách hỏi (khám tổng quát nếu khách không nói rõ)"


def norm(s):
    """Lowercase, NFC-safe, without Vietnamese diacritics -> for matching."""
    s = unicodedata.normalize("NFD", (s or "").lower()).replace("đ", "d")
    return re.sub(r"\s+", " ", "".join(c for c in s if unicodedata.category(c) != "Mn")).strip()


def nfc(s):
    return unicodedata.normalize("NFC", (s or "").strip())


def money(v):
    return f"{v:,}".replace(",", ".") + "đ"


# words every facility name has: left out when matching a reference facility by name
CHUNG_TEN = {"benh", "vien", "phong", "kham", "da", "khoa", "pkdk", "bv", "bvdk", "dk", "trung", "tam", "y", "te",
             "quoc", "cong", "ty", "tnhh", "chi", "nhanh", "co", "so", "he", "thong", "cs", "pk"}


def ten_co_so(name):
    """The distinctive words of a facility name, normalized ("Bệnh viện Đa khoa MEDLATEC" -> {"medlatec"})."""
    return set(re.findall(r"\w+", norm(name))) - CHUNG_TEN


def khoang_gia(tu, den):
    """The price range the customer asked for, in words: "từ 1.000.000đ đến 2.000.000đ", "dưới ...", "từ ... trở lên"."""
    tu, den = int(tu or 0), int(den or 0)
    if tu and den:
        return f"từ {money(tu)} đến {money(den)}"
    return f"dưới {money(den)}" if den else (f"từ {money(tu)} trở lên" if tu else "")


def read_csv(name):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [{k.strip(): nfc(v) for k, v in r.items() if k} for r in csv.DictReader(f)]


def read_md(folder, ma):
    """'## Heading' sections of du_lieu/<folder>/<ma>.md -> [(heading, text)]."""
    path = os.path.join(DATA_DIR, folder, f"{ma}.md")
    if not os.path.exists(path):
        return None
    sections, head, buf = [], None, []
    for line in open(path, encoding="utf-8").read().splitlines():
        if line.startswith("## "):
            if head is not None:
                sections.append((head, "\n".join(buf).strip()))
            head, buf = nfc(line[3:]), []
        elif head is not None:
            buf.append(line)
    if head is not None:
        sections.append((head, "\n".join(buf).strip()))
    return [(h, nfc(t)) for h, t in sections if t]


def read_loi_dan(path):
    """loi_dan.md -> {section name: text}. A section starts at '## <name>' and ends at the next '#' heading;
    lines starting with '>' explain the section and are left out; each other line is one paragraph."""
    out, name = {}, None
    for line in open(path, encoding="utf-8").read().splitlines():
        if line.startswith("#"):
            name = line[3:].strip() if line.startswith("## ") else None
            if name:
                out[name] = []
        elif name and line.strip() and not line.startswith(">"):
            out[name].append(line.strip())
    return {k: nfc("\n".join(v)) for k, v in out.items()}


def schedule_text(rows):
    by_day = {}
    for r in rows:
        by_day.setdefault(r["thu"], []).append(f"{r['buoi'].lower()} {r['khung_gio']}".strip())
    groups = []
    for d in THU:
        if d not in by_day:
            continue
        slots = "; ".join(by_day[d])
        if groups and groups[-1][1] == slots and THU.index(groups[-1][0][-1]) == THU.index(d) - 1:
            groups[-1][0].append(d)
        else:
            groups.append(([d], slots))
    return " | ".join((ds[0] if len(ds) == 1 else f"{ds[0]}–{ds[-1]}") + " " + s for ds, s in groups)


NEED_KEYS = {"goi_nhac_toi", "thu_tu", "cac_goi_truoc", "co_so_nhac_toi", "hoi_co_so", "can", "gioi_tinh", "tinh",
             "phuong_xa", "tuoi", "trieu_chung", "ca_nhan", "cac_loai_giay", "ngan_sach", "ngan_sach_tu", "hang_bang",
             "muc_dich", "tu_khoa", "ma_goi", "ma_co_so"}


_log_lock = threading.Lock()
VN_TZ = timezone(timedelta(hours=7))  # log times in Vietnam time (the container runs in UTC)


DAY_SPAN = re.compile(r"\b(Thứ|thứ|T)\s?([2-7])\s?[-–—]\s?(Thứ|thứ|T)\s?([2-7]|CN)\b|\b(T)([2-7])\s?[-–—]\s?(CN)\b")


def spell_day_spans(text):
    """'Thứ 2 - Thứ 6' -> 'từ Thứ 2 đến hết Thứ 6': the answer model read the dash as 'up to but not including'
    and wrote 'thứ 2 đến thứ 5'; the answer is streamed, so the data it reads must leave no room for that."""
    def spell(m):
        a, b = (m.group(2), m.group(4)) if m.group(2) else (m.group(6), m.group(7))
        last = "Chủ nhật" if b == "CN" else f"Thứ {b}"
        return f"từ Thứ {a} đến hết {last}"
    return DAY_SPAN.sub(spell, text) if isinstance(text, str) else text


def log_missing(kind, need, question, detail=""):
    """One row in nhat_ky/thieu_du_lieu.csv per question the data could not answer, for the data team.
    Questions from the test runner (Dify user "test-...") go to thieu_du_lieu_test.csv instead."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        test = str(need.get("_nguoi_dung", "")).startswith("test")
        path = os.path.join(LOG_DIR, "thieu_du_lieu_test.csv" if test else "thieu_du_lieu.csv")
        new = not os.path.exists(path)
        with _log_lock, open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["thoi_gian", "loai", "muc_dich", "tinh", "phuong_xa", "chi_tiet", "cau_hoi"])
            w.writerow([datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S"), kind, need.get("muc_dich") or "",
                        province_name(need.get("tinh")), need.get("phuong_xa") or "", detail, question])
    except OSError as e:  # never fail the lookup because of the log
        print(f"log_missing: {e}", flush=True)


SO_DIEN_THOAI = re.compile(r"(?<![\d+])(?:\+84|0)(\d{9,10})(?!\d)")


def so_dien_thoai(text):
    """The Vietnamese phone numbers in the customer's message, as 0xxxxxxxxx; "090 123 4567", "0901.234.567",
    "+84 901234567" all count. The BookingCare hotline (the customer quoting it back) is not the customer's."""
    flat = re.sub(r"(?<=\d)[ .\-](?=\d)", "", text or "")
    flat = re.sub(r"\+84[ .\-]?(?=\d)", "+84", flat)
    own = re.sub(r"\D", "", HOTLINE_BOOKINGCARE)
    out = []
    for m in SO_DIEN_THOAI.finditer(flat):
        so = "0" + m.group(1)
        if so != own and so not in out:
            out.append(so)
    return out


def log_lien_he(phones, need, question, situation, conversation):
    """One row in nhat_ky/lien_he_khach.csv per phone number a customer sends in the chat, for the BookingCare
    advisers (the customer is not told: the answer still asks them to fill the form or contact BookingCare). Test runner users ("test-...", "danh-sach-...") go to lien_he_khach_test.csv."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        test = str(need.get("_nguoi_dung", "")).startswith(("test", "danh-sach"))  # the two test runners
        path = os.path.join(LOG_DIR, "lien_he_khach_test.csv" if test else "lien_he_khach.csv")
        new = not os.path.exists(path)
        with _log_lock, open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["thoi_gian", "so_dien_thoai", "nhu_cau", "tinh", "phuong_xa", "co_so_nhac_toi",
                            "nguoi_dung", "hoi_thoai", "cau_hoi"])
            for so in phones:
                w.writerow([datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S"), so, situation,
                            province_name(need.get("tinh")), need.get("phuong_xa") or "",
                            ", ".join(map(str, need.get("co_so_nhac_toi") or [])),
                            need.get("_nguoi_dung") or "", conversation, question])
        return True
    except OSError as e:  # never fail the lookup because of the log
        print(f"log_lien_he: {e}", flush=True)
        return False


def last_json_object(text):
    """The last JSON object with the needs' keys in `text`: some models reason in plain text (which may
    contain braces) before writing the JSON."""
    dec, found = json.JSONDecoder(), None
    for pos in [m.start() for m in re.finditer(r"\{", text)]:
        try:
            obj, _ = dec.raw_decode(text, pos)
        except ValueError:
            continue
        if isinstance(obj, dict) and NEED_KEYS & set(obj):
            found = obj
    return found


PROVINCE_ALIASES = {
    "Thành phố Hồ Chí Minh": ["tp hcm", "tphcm", "hcm", "ho chi minh", "sai gon", "saigon", "tp ho chi minh", "hcmc"],
    "Hà Nội": ["ha noi", "hn", "hanoi", "tp ha noi"],
    "Nghệ An": ["nghe an", "vinh", "tp vinh"],
}


def province_name(s):
    """What the customer called the province -> the name used in the data ('' when not given)."""
    n = norm(s).replace(".", "").replace("thanh pho", "tp").strip()
    if not n:
        return ""
    for name, aliases in PROVINCE_ALIASES.items():
        if n == norm(name) or n in aliases or n.replace("tp ", "") in aliases:
            return name
    return nfc(s)  # another province: kept as is, so it matches nothing in the data instead of everything


# districts before the 2025 merger that customers still name ("Hà Đông", "quận Tân Phú", "Q9"): the province
QUAN_CU = {
    "Hà Nội": ["ba dinh", "hoan kiem", "tay ho", "long bien", "cau giay", "dong da", "hai ba trung", "hoang mai",
               "thanh xuan", "nam tu liem", "bac tu liem", "tu liem", "ha dong", "son tay", "dong anh", "gia lam",
               "soc son", "thanh tri", "me linh", "hoai duc", "thuong tin", "chuong my", "thanh oai", "quoc oai",
               "dan phuong", "phuc tho", "thach that", "ba vi", "my duc", "ung hoa", "phu xuyen", "my dinh",
               "trung hoa", "kim ma", "giai phong", "yen nghia", "linh dam", "trau quy"],
    "Thành phố Hồ Chí Minh": ["thu duc", "binh thanh", "go vap", "phu nhuan", "tan binh", "tan phu", "binh tan",
                              "binh chanh", "hoc mon", "cu chi", "nha be", "can gio"]
                             + [f"quan {i}" for i in range(1, 13)] + [f"q{i}" for i in range(1, 13)],
}


def phuong_cua_co_so(value):
    """The wards of a facility (normalized). phuong_xa may list several, separated by '|' (a facility on several sites)."""
    return [norm(re.sub(r"^(phường|xã|p\.)\s*", "", x.strip(), flags=re.I)) for x in str(value or "").split("|") if x.strip()]


def province_of_area(text):
    """The province of an old district / area named in the text, '' if none."""
    t = " " + " ".join(re.findall(r"\w+", norm(text))) + " "  # "Hà Đông, Thanh Xuân": commas split words too
    for name, areas in QUAN_CU.items():
        if any(f" {a} " in t for a in areas):
            return name
    return ""


def province_in(text):
    """A province named inside a sentence ("... tại hồ chí minh"), '' if none."""
    t = " " + norm(text) + " "
    for name, aliases in PROVINCE_ALIASES.items():
        if any(f" {w} " in t for w in aliases + [norm(name)]):
            return name
    return ""


def words(s):
    return re.findall(r"\w+", norm(s))


class Matcher:
    """Resolve a free-text mention ('EC1', 'HN3 VVIP Phúc Trường Minh', 'bệnh viện An Việt') to ids.

    A bracketed code such as EC1 matches exactly. Otherwise each id's text is scored by the share of the
    mention's word weight it contains, where weight = rarity (idf) of the word across all names, so
    generic words like 'gói', 'khám', 'bệnh viện' barely count. Too many equally good hits = ambiguous.
    """

    def __init__(self, texts, codes=None, group=None):
        """`group`: id -> the id a hit stands for (a public hospital's department or branch -> the hospital), so
        "Bệnh viện Đại học Y Dược TP.HCM" and its "Cơ sở 2" count as one hit."""
        import math
        self.group = group or {}
        self.docs = {m: set(words(t)) for m, t in texts.items()}
        self.codes = {m: {norm(c).replace(" ", "") for c in cs} for m, cs in (codes or {}).items()}
        df = {}
        for ws in self.docs.values():
            for w in ws:
                df[w] = df.get(w, 0) + 1
        n = max(len(self.docs), 1)
        self.idf = {w: math.log((n + 1) / (c + 0.5)) for w, c in df.items()}
        self.unknown = math.log((n + 1) / 0.5)  # a word found in no name weighs most and never matches

    STOP = {"o", "cua", "cho", "tai", "nay", "do", "kia", "la", "voi", "va", "ve", "minh", "toi", "ban", "nao"}

    def find(self, mention, limit=3, only=None):
        """`only` = ids to search among (e.g. the packages of the facility the customer named)."""
        m = norm(mention).strip("() ")
        if not m:
            return []
        compact = m.replace(" ", "")
        by_code = [i for i, cs in self.codes.items() if compact in cs and (only is None or i in only)]
        ws = [w for w in words(m) if w not in self.STOP]
        if by_code and len(ws) <= 1:
            return by_code[:limit]
        weight = {w: self.idf.get(w, self.unknown) for w in ws}
        total = sum(weight.values())
        if not total:
            return by_code[:limit]
        docs = self.docs if only is None else {i: d for i, d in self.docs.items() if i in only}
        if not docs:
            return []
        scored = sorted(((sum(weight[w] for w in ws if w in d) / total, i) for i, d in docs.items()),
                        reverse=True)
        best = scored[0][0]
        if best < 0.75:
            return by_code[:limit]
        hits = list(dict.fromkeys(self.group.get(i, i) for s, i in scored if s >= best - 0.05))
        if len(hits) > limit:  # ambiguous, e.g. 'gói khám tổng quát'
            return by_code[:limit]
        return list(dict.fromkeys(by_code + hits))[:limit]


class Catalog:
    """Everything loaded from DATA_DIR, plus the list of problems found while loading."""

    def __init__(self):
        self.issues = []  # (level, where, message)
        self.goi, self.co_so = {}, {}
        self.an = {}  # facilities in co_so.csv with hien_thi 0: known by name, no data shown yet
        self.load()

    def bad(self, level, where, msg):
        self.issues.append((level, where, msg))

    def load(self):
        co_so_rows = read_csv("co_so/co_so.csv")
        goi_rows = read_csv("goi/goi.csv")
        lich_rows = read_csv("goi/lich.csv") or []
        dm_rows = read_csv("goi/danh_muc.csv") or []
        if co_so_rows is None or goi_rows is None:
            self.bad("LỖI", "du_lieu", "thiếu goi/goi.csv hoặc co_so/co_so.csv")
            return

        for n, r in enumerate(co_so_rows, 2):
            where = f"co_so/co_so.csv dòng {n}"
            ma = r.get("ma_co_so", "")
            if not ma.isdigit():
                self.bad("LỖI", where, f"ma_co_so '{ma}' không phải số, bỏ qua dòng")
                continue
            if ma in self.co_so:
                self.bad("LỖI", where, f"trùng ma_co_so {ma}, bỏ qua dòng")
                continue
            if r.get("hien_thi", "1") == "0":
                self.an[ma] = dict(r, tinh=province_name(r.get("tinh", "")) or r.get("tinh", ""))
                continue
            if r.get("loai", "tu") not in ("tu", "cong"):
                self.bad("LỖI", where, f"loai '{r['loai']}' phải là tu / cong")
            if r.get("bao_hiem", "") not in ("co", "khong", ""):
                self.bad("CẢNH BÁO", where, f"bao_hiem '{r['bao_hiem']}' phải là co / khong / để trống")
            if not r.get("ten"):
                self.bad("LỖI", where, "thiếu tên cơ sở")
            if not r.get("dia_chi"):
                self.bad("CẢNH BÁO", where, "thiếu địa chỉ")
            sections = read_md("co_so/noi_dung", ma)
            if sections is None and r.get("loai") != "cong":  # a public hospital is described in kham_tong_quat.csv
                self.bad("CẢNH BÁO", where, f"không có file co_so/noi_dung/{ma}.md")
            self.co_so[ma] = dict(r, tinh=province_name(r.get("tinh", "")) or r.get("tinh", ""), sections=sections or [])
        for ma, c in self.co_so.items():
            if c.get("thuoc_he_thong") and c["thuoc_he_thong"] not in self.co_so:
                self.bad("CẢNH BÁO", f"co_so/co_so.csv mã {ma}", f"thuoc_he_thong {c['thuoc_he_thong']} không có trong co_so/co_so.csv")

        lich, dm = {}, {}
        for n, r in enumerate(lich_rows, 2):
            if r.get("thu") not in THU:
                self.bad("LỖI", f"goi/lich.csv dòng {n}", f"thu '{r.get('thu')}' phải là một trong {', '.join(THU)}")
                continue
            lich.setdefault(r.get("ma_goi", ""), []).append(r)
        for n, r in enumerate(dm_rows, 2):
            if not r.get("hang_muc"):
                self.bad("CẢNH BÁO", f"goi/danh_muc.csv dòng {n}", "thiếu hang_muc, bỏ qua")
                continue
            dm.setdefault(r.get("ma_goi", ""), []).append(r)

        for n, r in enumerate(goi_rows, 2):
            where = f"goi/goi.csv dòng {n}"
            ma = r.get("ma_goi", "")
            if not ma.isdigit():
                self.bad("LỖI", where, f"ma_goi '{ma}' không phải số, bỏ qua dòng")
                continue
            if ma in self.goi:
                self.bad("LỖI", where, f"trùng ma_goi {ma}, bỏ qua dòng")
                continue
            if r.get("hien_thi", "1") == "0":
                continue
            try:
                gia_tu = int(r["gia_tu"]) if r.get("gia_tu") else 0
                gia_den = int(r["gia_den"]) if r.get("gia_den") else 0
            except ValueError:
                self.bad("LỖI", where, "gia_tu / gia_den phải là số (đồng, không dấu chấm), bỏ qua dòng")
                continue
            if not gia_tu:
                self.bad("CẢNH BÁO", where, f"gói {ma} chưa có giá")
            if gia_den and gia_den < gia_tu:
                self.bad("LỖI", where, "gia_den nhỏ hơn gia_tu")
            if r.get("gioi_tinh", "") not in ("", "nam", "nu"):
                self.bad("LỖI", where, f"gioi_tinh '{r['gioi_tinh']}' phải là nam / nu / để trống")
            tags = [t for t in r.get("muc_dich", "").split("|") if t]
            for t in tags:
                if t not in MUC_DICH:
                    self.bad("LỖI", where, f"muc_dich '{t}' không hợp lệ ({', '.join(sorted(MUC_DICH))})")
            if not tags:
                self.bad("CẢNH BÁO", where, f"gói {ma} chưa có muc_dich, sẽ không được gợi ý theo nhu cầu")
            if not r.get("link", "").startswith("http"):
                self.bad("CẢNH BÁO", where, f"gói {ma} thiếu link thông tin chi tiết")
            fac = self.co_so.get(r.get("ma_co_so", ""))
            if r.get("ma_co_so") and not fac:
                self.bad("LỖI", where, f"ma_co_so {r['ma_co_so']} không có trong co_so/co_so.csv")
            if not fac and not r.get("noi_kham"):
                self.bad("CẢNH BÁO", where, f"gói {ma} không có ma_co_so lẫn noi_kham")
            sections = read_md("goi/noi_dung", ma)
            if sections is None:
                self.bad("CẢNH BÁO", where, f"không có file goi/noi_dung/{ma}.md")
            if ma not in lich:
                self.bad("CẢNH BÁO", where, f"gói {ma} không có lịch trong goi/lich.csv")
            if ma not in dm:
                self.bad("CẢNH BÁO", where, f"gói {ma} không có danh mục trong goi/danh_muc.csv")
            self.goi[ma] = dict(
                r, gia_tu=gia_tu, gia_den=gia_den, tags=tags, fac=fac,
                noi_kham=(fac["ten"] if fac else r.get("noi_kham", "")),
                tinh=(fac["tinh"] if fac and fac.get("tinh") else r.get("tinh", "")),
                lich=lich.get(ma, []), danh_muc=dm.get(ma, []), sections=sections or [])
            g = self.goi[ma]
            g["tuoi_min"], g["tuoi_max"] = gioi_han_tuoi(g["sections"])
            intro = norm(g.get("ten", "") + " " + " ".join(t for _, t in g["sections"][:1]))
            # "nữ độc thân" only; "độc thân và đã có gia đình" is for both
            g["doc_than"] = bool(CHO_DOC_THAN.search(intro)) and not DA_KET_HON.search(intro)
        # general notes per document type (kien_thuc/giay_to/<loai>.md); a package's own notes win over them
        self.luu_y_giay = {t: read_md("kien_thuc/giay_to", t) or [] for t in LOAI_GIAY}
        # driving-licence packages of facilities BookingCare does not partner with (lai_xe/goi_tham_khao.csv, from
        # he_thong/scripts/nhap_lai_xe.py): reference only, shown after the partner packages; and the facilities
        # the Department of Health lists for driving check-ups (lai_xe/co_so_duoc_kham.csv)
        self.goi_tk, self.co_so_lx = [], []
        path = os.path.join(DATA_DIR, "lai_xe", "goi_tham_khao.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig", newline="") as f:
                for r in csv.DictReader(f):
                    self.goi_tk.append(dict(r, gia_tu=int(r.get("gia_tu") or 0), gia_den=int(r.get("gia_den") or 0),
                                            _ten=ten_co_so(r["co_so"]), _dc=" " + " ".join(re.findall(r"\w+", norm(r["dia_chi"]))) + " "))
        path = os.path.join(DATA_DIR, "lai_xe", "co_so_duoc_kham.csv")
        if os.path.exists(path):
            with open(path, encoding="utf-8-sig", newline="") as f:
                self.co_so_lx = [dict(r, _ten=ten_co_so(r["ten"])) for r in csv.DictReader(f)]
        for t in sorted(LOAI_GIAY):
            if any(t in g["tags"] for g in self.goi.values()) and not self.luu_y_giay[t]:
                self.bad("CẢNH BÁO", f"kien_thuc/giay_to/{t}.md", f"chưa có lưu ý chung cho giấy khám {LOAI_GIAY_TEN[t]}")
        for ma, g in self.goi.items():
            if LOAI_GIAY & set(g["tags"]) and not g.get("thoi_gian_tra_ket_qua"):
                self.bad("CẢNH BÁO", f"goi/goi.csv mã {ma}", f"gói {ma} là gói giấy tờ nhưng chưa có thoi_gian_tra_ket_qua")
        for ma in set(lich) - set(self.goi):
            self.bad("CẢNH BÁO", "goi/lich.csv", f"ma_goi {ma} không có (hoặc đang ẩn) trong goi/goi.csv")
        for ma in set(dm) - set(self.goi):
            self.bad("CẢNH BÁO", "goi/danh_muc.csv", f"ma_goi {ma} không có (hoặc đang ẩn) trong goi/goi.csv")
        for g in self.goi.values():
            # word sets of the name and of each category item: a keyword matches a field holding all its words
            g["name_words"] = set(re.findall(r"\w+", norm(g["ten"])))
            g["item_words"] = [set(re.findall(r"\w+", norm(d["hang_muc"] + " " + d.get("mo_ta", ""))))
                               for d in g["danh_muc"]]
        # name matchers: what the customer calls a package / facility -> ids (rare words count more)
        self.goi_names = Matcher({m: g["ten"] + " " + g["noi_kham"] for m, g in self.goi.items()},
                                 codes={m: re.findall(r"\(([^()]+)\)", g["ten"]) for m, g in self.goi.items()})
        self.load_bv_cong()
        # facilities are also found by the other names people use ("BV 108", "Chợ Rẫy", "PK SG Healthcare")
        self.co_so_names = Matcher({m: re.sub(r"(?i)\bthành phố\b", " ", c["ten"]) + " " + c.get("ten_goi_khac", "").replace("|", " ")
                                    for m, c in self.co_so.items()},
                                   group={m: self.benh_vien_chinh(m) for m in self.co_so})
        self.an_names = Matcher({m: c["ten"] + " " + c.get("ten_goi_khac", "").replace("|", " ")
                                 for m, c in self.an.items()})

        # checklist of Mẫu số 03 (Thông tư 25/2026): used for company checks and questions about Thông tư 25
        head = {}
        path = os.path.join(DATA_DIR, "kien_thuc", "doanh_nghiep", "danh_muc_tt25.md")
        if os.path.exists(path):
            m = re.match(r"---\n(.*?)\n---", open(path, encoding="utf-8").read(), re.S)
            head = dict(re.findall(r"^(\w+):\s*(.*)$", m.group(1), re.M)) if m else {}
        self.tt25 = dict(tieu_de=nfc(head.get("tieu_de", "")), nguon=head.get("nguon", ""),
                         sections=read_md("kien_thuc/doanh_nghiep", "danh_muc_tt25") or [])
        if not self.tt25["sections"]:
            self.bad("CẢNH BÁO", "kien_thuc/doanh_nghiep/danh_muc_tt25.md", "chưa có danh mục khám theo Thông tư 25")
        elif not self.tt25["nguon"].startswith("http"):
            self.bad("CẢNH BÁO", "kien_thuc/doanh_nghiep/danh_muc_tt25.md", "thiếu dòng nguon: <link mẫu giấy khám>")

        # facilities that take company health checks through BookingCare (the only ones offered to companies)
        self.doanh_nghiep = []
        for n, r in enumerate(read_csv("co_so/hop_tac_doanh_nghiep.csv") or [], 2):
            where = f"co_so/hop_tac_doanh_nghiep.csv dòng {n}"
            if r.get("hien_thi", "1") == "0":
                continue
            fac = self.co_so.get(r.get("ma_co_so", ""))
            if not fac:
                self.bad("LỖI", where, f"ma_co_so '{r.get('ma_co_so', '')}' không có (hoặc đang ẩn) trong co_so/co_so.csv, bỏ qua dòng")
                continue
            if r.get("hinh_thuc_kham") and r["hinh_thuc_kham"] not in HINH_THUC_KHAM:
                self.bad("CẢNH BÁO", where, f"hinh_thuc_kham '{r['hinh_thuc_kham']}' phải là {' / '.join(HINH_THUC_KHAM)} "
                                           "hoặc để trống")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", r.get("ngay_cap_nhat", "")):
                self.bad("CẢNH BÁO", where, "ngay_cap_nhat phải có dạng 2026-11-02")
            if r.get("gia_tu") and not r["gia_tu"].isdigit():
                self.bad("LỖI", where, "gia_tu phải là số (đồng / người, không dấu chấm), bỏ qua giá")
                r["gia_tu"] = ""
            # the facility's own company package on BookingCare (goi.csv, muc_dich doanh_nghiep): its price is the
            # reference price when the partner sheet gives none
            goi_dn = sorted((g for g in self.goi.values() if g.get("ma_co_so") == r["ma_co_so"]
                             and "doanh_nghiep" in g["tags"] and g["gia_tu"]), key=lambda g: g["gia_tu"])
            if not r.get("gia_tu") and not r.get("khoang_gia") and goi_dn:
                r["gia_tu"] = str(goi_dn[0]["gia_tu"])
            self.doanh_nghiep.append(dict(r, goi_dn=goi_dn, gia_tu=int(r.get("gia_tu") or 0), ten=r.get("ten_hien_thi") or fac["ten"], tinh=fac.get("tinh", ""),
                                          ten_goi_khac=fac.get("ten_goi_khac", ""), fac=fac,
                                          chi_tiet=read_md("kien_thuc/doanh_nghiep/co_so", r["ma_co_so"]) or []))
        # shared Q&A: every file in kien_thuc/hoi_dap/ (one topic per file, one question per "## " section)
        folder = os.path.join(DATA_DIR, "kien_thuc", "hoi_dap")
        self.hoi_dap = [s for f in sorted(os.listdir(folder)) if f.endswith(".md")
                        for s in read_md("kien_thuc/hoi_dap", f[:-3]) or []] if os.path.isdir(folder) else []
        # knowledge files carry "duyet: co" once PO / the advisers approved the wording
        for root, _, files in os.walk(os.path.join(DATA_DIR, "kien_thuc")):
            for f in sorted(files):
                if not f.endswith(".md"):
                    continue
                path = os.path.join(root, f)
                text = open(path, encoding="utf-8").read()
                m = re.match(r"---\n(.*?)\n---", text, re.S)
                approved = bool(m and re.search(r"^duyet:\s*co\s*$", m.group(1), re.M))
                if not approved and read_md(os.path.relpath(root, DATA_DIR), f[:-3]):
                    self.bad("CẢNH BÁO", os.path.relpath(path, DATA_DIR), "nội dung chưa được duyệt (duyet: co)")
        # company notes from Product / Business (kien_thuc/doanh_nghiep/luu_y.md); empty sections are left out
        self.luu_y_dn = read_md("kien_thuc/doanh_nghiep", "luu_y") or []
        self.dn_names = Matcher({i: r["ten"] + " " + r.get("ten_goi_khac", "").replace("|", " ")
                                 for i, r in enumerate(self.doanh_nghiep)})
        # the instructions of each situation: every section the code asks for must exist, and each section in
        # the file should be used (by the code, or inserted as {name} into another section)
        where = "he_thong/catalog/loi_dan.md"
        self.loi_dan = read_loi_dan(LOI_DAN) if os.path.exists(LOI_DAN) else {}
        if not self.loi_dan:
            self.bad("LỖI", where, "không đọc được file lời dặn")
        src = open(os.path.abspath(__file__), encoding="utf-8").read()
        for k in sorted(set(re.findall(r'(?:ghi_chu|ld)\(\s*"([\w.]+)"', src)) - set(self.loi_dan)):
            self.bad("LỖI", where, f"thiếu mục ## {k}")
        inserted = set(re.findall(r"\{(\w+)\}", "\n".join(self.loi_dan.values())))
        for k in sorted(k for k in self.loi_dan if f'"{k}"' not in src and k not in inserted):
            self.bad("CẢNH BÁO", where, f"mục ## {k} không được dùng ở đâu")

    def benh_vien_chinh(self, ma):
        """A public hospital's department or branch -> the hospital (its block lists them all); others: itself."""
        seen = set()
        while (self.co_so.get(ma, {}).get("loai") == "cong" and self.co_so[ma].get("thuoc_he_thong") in self.co_so
               and ma not in seen):
            seen.add(ma)
            ma = self.co_so[ma]["thuoc_he_thong"]
        return ma

    def find_co_so(self, mention, limit=3, tinh=""):
        hits = self._find_co_so(mention, limit)
        if not hits and not re.fullmatch(r"\s*#\d+\s*", str(mention)):
            # too many hospitals share the name ("đại học y": Hà Nội, TP.HCM, Huế, Cần Thơ, Thái Bình): the
            # province the customer gave decides; without one, public hospitals only -> asked which one
            for v in [mention] + self.name_variants(mention):
                wide = self.narrow(mention, self.co_so_names.find(v, 8))  # the place in the mention, no "phòng khám"
                said = province_in(mention) or province_of_area(mention)
                if said:
                    wide = [h for h in wide if norm(self.co_so[h].get("tinh", "")) == norm(said)]
                if tinh:
                    wide = [h for h in wide if norm(self.co_so[h].get("tinh", "")) == norm(tinh)]
                if wide and (len(wide) <= limit or not tinh and all(self.co_so[h].get("loai") == "cong" for h in wide)):
                    hits = wide
                    break
        hits = list(dict.fromkeys(self.benh_vien_chinh(h) for h in hits))
        n = norm(mention)
        if re.search(r"\b(phong kham|pk)\b", n) and not re.search(r"\b(benh vien|bv)\b", n):
            # "phòng khám X" is a clinic: a public hospital called "Bệnh viện X" is not that clinic
            hits = [h for h in hits if not (self.co_so[h].get("loai") == "cong" and norm(self.co_so[h]["ten"]).startswith("benh vien"))]
        return hits

    def _find_co_so(self, mention, limit):
        m = re.fullmatch(r"#(\d+)", str(mention).strip())
        if m:  # a facility id: the page the customer is on (the chatflow's "trang" input)
            return [m.group(1)] if m.group(1) in self.co_so else []
        mention = re.sub(r"\b(tp\.?\s?hcm|tphcm|hcm)\b", "tp hcm", str(mention), flags=re.I)  # "tphcm" written together
        mention = re.sub(r"\b(hn|hnoi)\b", "Hà Nội", mention, flags=re.I)  # "BV Đại học y HN" (not "HN3", a package code)
        exact = self.alias_exact(mention)
        if exact:  # "bệnh viện 115" is Nhân dân 115 (its alias), not Gia An 115
            return exact
        hits = self.co_so_names.find(mention, limit) or self.find_initials(BRANCH.sub("", mention), limit)
        if not hits and BRANCH.search(mention):  # the branch may be in the name ("Hồng Ngọc - Yên Ninh"), else drop it
            hits = (self.co_so_names.find(re.sub(r"\b(cơ sở|co so|cs|chi nhánh|chi nhanh)\b", " ", mention, flags=re.I), limit)
                    or self.co_so_names.find(BRANCH.sub("", mention), limit))
        if not hits:
            # extra words keep a name from matching: "Trung tâm khám sức khoẻ định kỳ, bệnh viện TWQĐ 108",
            # "bệnh viện Đại học y dược quận 5 Thành phố Hồ Chí Minh", "Medlatec Hà Nội – Thanh Xuân – Phòng Khám"
            for v in self.name_variants(mention):
                hits = self.co_so_names.find(v, limit) or self.alias_exact(v)
                if hits:
                    break
        return self.narrow(mention, hits)

    def narrow(self, mention, hits):
        """Several equally good hits: the province / old district the mention names decides; "Bệnh viện Đại học Y
        Dược" is the hospital, not "Phòng khám Bệnh viện Đại học Y Dược 1", unless the customer says phòng khám."""
        where = province_in(mention) or province_of_area(mention)
        if where:
            hits = [h for h in hits if not self.co_so[h].get("tinh") or norm(self.co_so[h].get("tinh", "")) == norm(where)]
        if len(hits) < 2:
            return hits
        # "bệnh viện E": the hospital, not also its departments / branches ("Trung tâm Tim mạch Bệnh viện E")
        hits = [h for h in hits if not (self.co_so[h].get("loai") == "cong" and self.co_so[h].get("thuoc_he_thong") in hits)]
        if len(hits) < 2:
            return hits
        if not re.search(r"\b(phong kham|pk)\b", norm(mention)):
            hits = [h for h in hits if not norm(self.co_so[h]["ten"]).startswith("phong kham")] or hits
        return hits

    ABBR = [(r"\btw\s*qd\b|\btwqd\b", "trung uong quan doi"), (r"\bqd\b", "quan doi"), (r"\bdhyd\b", "dai hoc y duoc"),
            (r"\bdhy\b", "dai hoc y"), (r"\bdh\s+yd\b|\bdh\s+y\s+duoc\b", "dai hoc y duoc"),
            (r"\bdh\s+y\b", "dai hoc y"), (r"\bdhqg\b", "dai hoc quoc gia"), (r"\bbv\b", "benh vien")]
    NOISE = (r"\b(trung tam|khoa|phong) kham( suc khoe)?( dinh ky| theo yeu cau| tong quat)?\b|\bco so\b.*$|"
             r"\b(quan|q|phuong|p|huyen|xa)\s*\d+\b|\b(thanh pho|tp)\s+(ho chi minh|hcm|ha noi)\b|\bho chi minh\b|"
             r"\bsai gon\b|\bha noi\b|\btp hcm\b|\bphong kham\b$")

    def name_variants(self, mention):
        """Shorter forms of a facility mention: abbreviations spelled out, place words dropped, each part
        between commas / dashes on its own (longest first)."""
        n = norm(mention)
        for pat, full in self.ABBR:
            n = re.sub(pat, full, n)
        out = [n]
        bare = " ".join(re.sub(self.NOISE, " ", n).split())
        for area in sum(QUAN_CU.values(), []):
            bare = re.sub(rf"\b{area}\b", " ", bare)
        out.append(" ".join(bare.split()))
        before = re.split(r"\s(?:o|tai|gan|canh)\s", n)[0]  # "... đại học quốc gia ở ktx mễ trì"
        if before != n:
            out.append(" ".join(re.sub(self.NOISE, " ", before).split()))
        parts = [" ".join(re.sub(self.NOISE, " ", x).split()) for x in re.split(r"[,;–—|]|\s-\s", n)]
        out += sorted((x for x in parts if len(x) > 2), key=len, reverse=True)
        return [v for v in dict.fromkeys(out) if v and v != norm(mention)]

    def alias_exact(self, mention):
        """A facility whose other name (ten_goi_khac) is exactly what the customer wrote."""
        n = re.sub(r"^(benh vien|bv|vien)\s+", "bv ", norm(mention).strip())
        hits = [m for m, c in self.co_so.items()
                if n in {re.sub(r"^(benh vien|vien)\s+", "bv ", norm(a).strip()) for a in c.get("ten_goi_khac", "").split("|") if a}]
        return hits[:1] if len(hits) == 1 else []

    def find_initials(self, mention, limit):
        """'BVĐHY', 'ĐHYHN', 'BVBM': the first letters of a facility's name (with or without 'bệnh viện')."""
        token = norm(mention).replace(" ", "")
        if not re.fullmatch(r"[a-z]{3,8}", token):
            return []
        hits = []
        for m, c in self.co_so.items():
            initials = "".join(w[0] for w in words(c["ten"]))
            if initials.startswith(token) or initials[2:].startswith(token) and initials.startswith("bv"):
                hits.append(m)
        hits = list(dict.fromkeys(self.benh_vien_chinh(h) for h in hits))  # a hospital and its branches: one
        return hits if 0 < len(hits) <= limit else []

    def load_bv_cong(self):
        """co_so/kham_tong_quat.csv: the general check-up data of each public hospital (loai cong in co_so.csv),
        one row per hospital, by ma_co_so."""
        rows = {r.get("ma_co_so", ""): (n, r) for n, r in enumerate(read_csv("co_so/kham_tong_quat.csv") or [], 2)}
        imported_missing = 0
        for ma, (n, r) in rows.items():
            fac = self.co_so.get(ma)
            if not fac:  # hidden in co_so.csv (data still being prepared) or a wrong id
                continue
            where = f"co_so/kham_tong_quat.csv dòng {n}"
            if fac.get("loai") != "cong":
                self.bad("CẢNH BÁO", where, f"cơ sở {ma} không phải bệnh viện công (loai cong) trong co_so/co_so.csv, bỏ qua")
                continue
            missing = [label for col, label, need in KTQ_FIELDS if need and not r.get(col)]
            if missing and r.get("nguon", "").startswith(NGUON_NHAP_BV_CONG):
                imported_missing += 1  # imported from a hospital list: one warning for all of them
            elif missing:
                self.bad("CẢNH BÁO", where, f"{fac['ten']}: chưa có {', '.join(missing)} (bắt buộc)")
            fac.update(ktq=[(label, r.get(col, "") + (f" (link: {r['link_chi_phi']})" if col == "chi_phi" and r.get("link_chi_phi") else ""))
                            for col, label, _ in KTQ_FIELDS],
                       ktq_nguon=r.get("nguon", ""), ktq_ngay=r.get("ngay_cap_nhat", ""))
        if imported_missing:
            self.bad("CẢNH BÁO", "co_so/kham_tong_quat.csv", f"{imported_missing} bệnh viện công nhập từ danh sách "
                                                             "(nhap_bv_cong.py) chưa đủ các mục bắt buộc")
        for ma, c in self.co_so.items():
            if c.get("loai") == "cong" and "ktq" not in c:
                self.bad("CẢNH BÁO", f"co_so/co_so.csv mã {ma}", f"{c['ten']} là bệnh viện công nhưng chưa có dòng trong "
                                                                "co_so/kham_tong_quat.csv")
                c["ktq"] = [(label, "") for _, label, _ in KTQ_FIELDS]

    # ---------- rendering (what the answer LLM reads)
    def gia(self, g):
        if not g["gia_tu"]:
            return "Liên hệ"
        return f"{money(g['gia_tu'])} – {money(g['gia_den'])}" if g["gia_den"] else money(g["gia_tu"])

    @staticmethod
    def lead_link(g):
        return LEAD_LINK_DOANH_NGHIEP if "doanh_nghiep" in g["tags"] else LEAD_LINK.format(ma=g["ma_goi"])

    def cta_dat_lich(self, g):
        """The package's booking link as a Dify markdown button: 'Đặt lịch gói EC1' (the code in the name, if any)."""
        code = re.findall(r"\(([^()]{1,12})\)", g["ten"])
        label = f"Đặt lịch gói {code[-1]}" if code else "Đặt lịch gói này"
        return f'<button data-variant="primary" data-link="{self.lead_link(g)}">{label}</button>'

    def render_goi(self, g, phan):
        out = [f"=== GÓI KHÁM mã {g['ma_goi']} ===", f"Tên gói: {g['ten']}",
               f"Nơi khám: {g['noi_kham'] or 'KHÔNG CÓ TRONG DỮ LIỆU'}"]
        if g["fac"] and g["fac"].get("dia_chi"):
            out.append(f"Địa chỉ: {g['fac']['dia_chi']}")
        # no schedule: no line at all, or the answer volunteers "chưa có thông tin về lịch khám" on every listing
        out += [f"Tỉnh/thành: {g['tinh'] or 'KHÔNG CÓ TRONG DỮ LIỆU'}", f"Giá: {self.gia(g)}"]
        if schedule_text(g["lich"]):
            out.append(f"Lịch khám: {schedule_text(g['lich'])}")
        out += [
                f"Link đặt lịch / để lại thông tin tư vấn: {self.lead_link(g)}",
                f"Nút đặt lịch (chép NGUYÊN dòng này khi đưa link đặt lịch): {self.cta_dat_lich(g)}",
                f"Link xem thông tin chi tiết gói: {g.get('link') or 'KHÔNG CÓ TRONG DỮ LIỆU'}"]
        loai = [t for t in g["tags"] if t in LOAI_GIAY]
        if loai:
            out.append("Loại giấy khám: " + ", ".join(LOAI_GIAY_TEN[t] for t in loai))
        if loai or g.get("thoi_gian_tra_ket_qua"):
            out.append(f"Thời gian trả kết quả: {g.get('thoi_gian_tra_ket_qua') or 'KHÔNG CÓ TRONG DỮ LIỆU'}")
        sec = {norm(h): (h, t) for h, t in g["sections"]}
        for key in phan:
            title = GOI_PHAN[key]
            if key == "luu_y_giay":
                if not loai:
                    continue
                own = sec.get(norm(title))
                out += ["", "Lưu ý giấy tờ riêng của gói (khi khác lưu ý chung thì theo phần này):",
                        own[1] if own else "KHÔNG CÓ TRONG DỮ LIỆU"]
                for t in loai:
                    general = self.luu_y_giay.get(t) or []
                    out += ["", f"Lưu ý chung cho giấy khám {LOAI_GIAY_TEN[t]}:"]
                    out += [f"- {h}: {txt}" for h, txt in general] or ["KHÔNG CÓ TRONG DỮ LIỆU"]
                continue
            if key == "danh_muc":
                if not g["danh_muc"]:
                    out += ["", f"{title}: KHÔNG CÓ TRONG DỮ LIỆU"]
                    continue
                out += ["", f"{title}:"]
                groups = {}  # groups keep their file order; thu_tu orders items inside a group
                for d in g["danh_muc"]:
                    groups.setdefault(d["nhom"] or "Khác", []).append(d)
                for group, items in groups.items():
                    out.append(f"[{group}]")
                    items.sort(key=lambda d: int(d["thu_tu"]) if d.get("thu_tu", "").isdigit() else 0)
                    out += [f"- {d['hang_muc']}" + (f": {d['mo_ta']}" if d.get("mo_ta") else "") for d in items]
            else:
                hit = sec.get(norm(title))
                out += ["", f"{title}:", hit[1]] if hit else ["", f"{title}: KHÔNG CÓ TRONG DỮ LIỆU"]
        return "\n".join(out)

    def render_co_so(self, c, doanh_nghiep=False):
        bh = {"co": "có", "khong": "không"}.get(c.get("bao_hiem", ""), "KHÔNG CÓ TRONG DỮ LIỆU")
        out = [f"=== CƠ SỞ Y TẾ mã {c['ma_co_so']} ===", f"Tên cơ sở: {c['ten']}",
               f"Địa chỉ: {c.get('dia_chi') or 'KHÔNG CÓ TRONG DỮ LIỆU'}",
               f"Tỉnh/thành: {c.get('tinh') or 'KHÔNG CÓ TRONG DỮ LIỆU'}",
               f"Điện thoại: {c.get('dien_thoai') or 'KHÔNG CÓ TRONG DỮ LIỆU'}"]
        if c.get("loai") != "cong":  # a public hospital says whether insurance applies in its check-up data
            out.append(f"Bảo hiểm: {bh}")
        parent = self.co_so.get(c.get("thuoc_he_thong", ""))
        if parent:
            out.append(f"Thuộc hệ thống: {parent['ten']}")
        if c.get("link"):
            out.append(f"Trang cơ sở trên BookingCare (chỉ để xem thông tin, không phải link đặt lịch): {c['link']}")
        if c.get("loai") == "cong" and doanh_nghiep:
            # company enquiry at a public hospital: contact + booking channels only, never personal check-up prices
            out += ["Loại cơ sở: bệnh viện công. Hiện BookingCare chưa hỗ trợ đăng ký khám doanh nghiệp tại đây (nói với khách "
                    "đúng như vậy, KHÔNG nêu lý do 'vì đây là bệnh viện công'); "
                    "khách liên hệ trực tiếp bệnh viện qua điện thoại / website dưới đây.",
                    f"Website bệnh viện: {c.get('website') or 'KHÔNG CÓ TRONG DỮ LIỆU'}"]
            for label, value in c.get("ktq", []):
                if label in ("Nơi đăng ký", "Cách đặt lịch") and value:
                    out.append(f"- {label}: {value}")
            return "\n".join(out)
        if c.get("loai") == "cong":
            out += ["Loại cơ sở: bệnh viện công. Hiện BookingCare chưa hỗ trợ đặt lịch tại đây, KHÔNG nêu lý do 'vì đây là bệnh viện "
                    "công' với khách (gói của chính bệnh "
                    "viện, nếu có, nằm trong dữ liệu dưới đây); chỉ hướng dẫn khách tự đi khám.",
                    f"Website bệnh viện: {c.get('website') or 'KHÔNG CÓ TRONG DỮ LIỆU'}", "",
                    "KHÁM TỔNG QUÁT TẠI BỆNH VIỆN NÀY:"]
            out += [f"- {label}: {value or 'KHÔNG CÓ TRONG DỮ LIỆU'}" for label, value in c["ktq"]]
            if c.get("ktq_nguon"):
                out.append(f"Nguồn thông tin khám tổng quát: {c['ktq_nguon']}")
        for h, t in c["sections"]:
            if c.get("loai") == "cong" or not KHAM_LE_SECTION.match(h):
                out += ["", f"{h}:", t]
        if not c["sections"]:
            out += ["", "Thông tin giới thiệu, đường đi, thủ tục: KHÔNG CÓ TRONG DỮ LIỆU"]
        return "\n".join(out)

    def danh_sach(self):
        fac = "\n".join(f"[{m}] {c['ten']}" for m, c in self.co_so.items())
        goi = "\n".join(f"[{m}] {g['ten']} | {g['noi_kham'] or '—'} | {g['tinh'] or '—'} | {self.gia(g)}"
                        for m, g in self.goi.items())
        return ("DANH SÁCH CƠ SỞ ([mã cơ sở] tên):\n" + fac +
                "\n\nDANH SÁCH GÓI ([mã] tên gói | nơi khám | tỉnh/thành | giá):\n" + goi)

    # ---------- finding packages
    def facility_family(self, fac_ids):
        """The facilities themselves plus, for a system (e.g. Hệ thống Bệnh viện Hồng Ngọc), its branches."""
        fam = set(fac_ids)
        fam |= {m for m, c in self.co_so.items() if c.get("thuoc_he_thong") in fac_ids}
        return fam

    def loc(self, need, budget_factor=1.0, at=None):
        """Packages matching the needs, best first. `at` = set of facility ids to list packages of (then
        special-purpose packages are kept, since the customer asked for everything at that place)."""
        gender, province = need.get("gioi_tinh") or "", province_name(need.get("tinh"))
        budget = int(need.get("ngan_sach") or 0) * budget_factor
        # lower bound of a price range ("trên 3 triệu", "từ 1 đến 2 triệu"); a looser search lowers it too
        budget_min = int(need.get("ngan_sach_tu") or 0) / budget_factor
        purpose = need.get("muc_dich") or ""
        tuoi = int(need.get("tuoi") or 0)
        married = bool(need.get("_da_ket_hon"))
        ward = norm(re.sub(r"^(phường|xã|p\.)\s*", "", need.get("phuong_xa") or "", flags=re.I))
        keywords = [set(re.findall(r"\w+", norm(k))) for k in need.get("tu_khoa") or [] if norm(k)]
        # the same words with their tone marks: "thận" (kidney) must not match "độc thân" in a package name
        accented = [set(re.findall(r"\w+", unicodedata.normalize("NFC", str(k)).lower())) for k in need.get("tu_khoa") or [] if norm(k)]
        scored = []
        for m, g in self.goi.items():
            if at is not None and g.get("ma_co_so") not in at:
                continue
            if gender and g.get("gioi_tinh") and g["gioi_tinh"] != gender:
                continue
            if province and norm(g["tinh"]) != norm(province):
                continue
            if need.get("_nguoi_lon") and g.get("tuoi_max", 200) < 30:
                continue  # "người lớn": not a package for the under-30s
            if tuoi and not (g.get("tuoi_min", 0) <= tuoi <= g.get("tuoi_max", 200)):
                continue  # the package states an age range the customer is outside of
            if married and purpose != "tien_hon_nhan" and ("tien_hon_nhan" in g["tags"] or g.get("doc_than")):
                continue  # married customer: no pre-marriage / singles package
            if budget and g["gia_tu"] > budget:
                continue
            if budget_min and (not g["gia_tu"] or (g.get("gia_den") or g["gia_tu"]) < budget_min):
                continue  # below the range (a package without a published price is not known to be in it)
            name_acc = set(re.findall(r"\w+", unicodedata.normalize("NFC", g.get("ten", "")).lower()))
            in_name = [k for k, a in zip(keywords, accented) if k <= g["name_words"]
                       and (a <= name_acc or a == k)]  # a keyword written without marks still matches by norm
            in_items = [k for k in keywords if k not in in_name and any(k <= w for w in g["item_words"])]
            if keywords and not (in_name or in_items):
                continue
            # A package NAMED after what the customer asked for (e.g. 'Tầm soát Tuyến Giáp') is kept even if
            # the extracted purpose doesn't match its tag; purpose only filters the rest. A package that HAS
            # the asked item ('nội soi dạ dày') is kept too unless the purpose is a special one (giấy tờ...).
            if not in_name and not (in_items and purpose not in SPECIAL):
                if purpose in MUC_DICH and purpose != "chuyen_khoa":
                    if purpose not in g["tags"]:
                        continue
                elif SPECIAL & set(g["tags"]) and at is None:
                    continue
            score = 3 * len(in_name) + len(in_items)
            score += 1 if gender and g.get("gioi_tinh") == gender else 0
            # gender / need not known yet: singles and cancer screening packages come after all the others
            later = int(not gender and not in_name and bool(g.get("doc_than") or ("ung_thu" in g["tags"] and purpose != "ung_thu")))
            if self.o_khu_vuc(g, ward, need.get("_quan")):  # in the ward / old district the customer named first
                score += 2
            scored.append((later, -score, g["gia_tu"], m))
        return [m for *_, m in sorted(scored)]

    def loc_tham_khao(self, need):
        """Reference driving-licence packages in the customer's province and price range: those in the ward / old
        district the customer named first, then by price (no published price last)."""
        province = province_name(need.get("tinh"))
        if not province:
            return []
        lo, hi = int(need.get("ngan_sach_tu") or 0), int(need.get("ngan_sach") or 0)
        ward = norm(re.sub(r"^(phường|xã|p\.)\s*", "", need.get("phuong_xa") or "", flags=re.I))
        quan = need.get("_quan") or []
        out = []
        for r in self.goi_tk:
            if norm(r["tinh"]) != norm(province):
                continue
            if (lo or hi) and not r["gia_tu"]:
                continue  # a price range asked: only places with a known price
            if (hi and r["gia_tu"] > hi) or (lo and (r["gia_den"] or r["gia_tu"]) < lo):
                continue
            area = bool(ward and f" {ward} " in r["_dc"]) or any(f" {a} " in r["_dc"] for a in quan)
            out.append(((not area, not r["gia_tu"], r["gia_tu"], r["ma"]), r))
        return [r for _, r in sorted(out, key=lambda x: x[0])]

    def tham_khao_theo_ten(self, names):
        """Reference packages of the facilities named (a name matches when the distinctive words of one are all
        in the other)."""
        keys = [k for k in (ten_co_so(n) for n in names if n) if k]
        return [r for r in self.goi_tk if r["_ten"] and any(k <= r["_ten"] or r["_ten"] <= k for k in keys)]

    def duoc_kham_lai_xe(self, name):
        k = ten_co_so(name)
        return next((r for r in self.co_so_lx if k and r["_ten"] and (k <= r["_ten"] or r["_ten"] <= k)), None)

    def render_tham_khao(self, r):
        """A reference package as the answer step reads it (no booking link: BookingCare does not partner here)."""
        if r["gia_tu"]:
            gia = f"{money(r['gia_tu'])} – {money(r['gia_den'])}" if r["gia_den"] else money(r["gia_tu"])
            # the sheet's own wording is kept only when it changes what the price covers ("chưa gồm XN", "phí khám")
            if re.search(r"chưa gồm|chua gom|phí khám|phi kham|trọn gói|tron goi", r.get("gia_ghi") or "", re.I):
                gia += f" ({r['gia_ghi']})"
        else:
            gia = "chưa có giá (khách gọi cơ sở để hỏi)"
        lx = self.duoc_kham_lai_xe(r["co_so"])
        lines = [f"=== GÓI THAM KHẢO {r['ma']} (không có nút đặt lịch) ===",
                 f"Tên gói: {r['ten_goi']}", f"Nơi khám: {r['co_so']}", f"Địa chỉ: {r['dia_chi']}",
                 f"Tỉnh/thành: {r['tinh']}", f"Giá tham khảo: {gia}",
                 f"Đối tượng / hạng bằng: {r['doi_tuong']}", f"Danh mục khám: {r['danh_muc']}",
                 f"Lưu ý khi đi khám: {r['luu_y']}", f"Giờ làm việc: {r.get('gio_lam_viec') or 'KHÔNG CÓ TRONG DỮ LIỆU'}",
                 f"Điện thoại của cơ sở: {r.get('dien_thoai') or 'KHÔNG CÓ TRONG DỮ LIỆU'}"]
        if r.get("ghi_chu"):
            lines.append(f"Ghi chú: {r['ghi_chu']}")
        if lx:
            lines.append(f"Danh sách Sở Y tế: có tên trong danh sách cơ sở đủ điều kiện khám sức khỏe lái xe ({lx['tinh']})")
        return "\n".join(lines)

    @staticmethod
    def o_khu_vuc(g, ward, quan):
        """The package's facility is in the ward (normalized, its phuong_xa or address) or one of the old districts
        (normalized, in its address) the customer named."""
        fac = g.get("fac") or {}
        if not fac:
            return False
        addr = " " + " ".join(re.findall(r"\w+", norm(fac.get("dia_chi", "")))) + " "
        if ward and (ward in phuong_cua_co_so(fac.get("phuong_xa")) or f" {ward} " in addr):
            return True
        return any(f" {a} " in addr for a in quan or [])

    # ---------- lookup: read the request, understand the turn, pick ONE situation, answer it
    def tra_cuu(self, raw):
        """raw = optional first line 'TRUOC: <ids shown in the previous answer>' + the needs JSON
        + optional last line 'HOI: <the customer's question>' (only used for the missing-data log)."""
        t = self.doc_yeu_cau(raw)
        if t is None:
            return self.ket_qua("khong_doc_duoc", [], [], [self.ghi_chu("khong_doc_duoc")])
        self.hieu(t)
        ten = self.chon_tinh_huong(t)
        if t.no_data and ten != "hoi_benh_vien_nao":
            log_missing("co_so_chua_co_du_lieu", t.need_no_data, t.question, t.no_data_names)
        out = getattr(self, "tl_" + ten)(t)
        now = re.search(r"TÌNH HUỐNG: (\S+)", out).group(1)
        extra = []
        if HOI_LIEN_HE.search(t.nq) and not now.startswith(("doanh_nghiep", "bv_cong")) and now != "trieu_chung":
            extra.append(self.ghi_chu("lien_he_ca_nhan"))
        phones = so_dien_thoai(t.question)
        if phones:
            log_lien_he(phones, t.need, t.question, now, t.need.get("_hoi_thoai") or "")  # for the advisers only
            dn = now.startswith("doanh_nghiep")
            extra.append(self.ghi_chu(
                "da_luu_sdt",
                cach_de_lai=("điền form ở nút Đăng ký tư vấn khám doanh nghiệp, hoặc chủ động chat Zalo / gọi BookingCare" if dn
                             else "để lại thông tin qua nút đặt lịch của gói (nếu có) hoặc nút Để lại thông tin tư vấn, hoặc chủ động chat Zalo / gọi BookingCare"),
                vi_du=("điền form đăng ký tư vấn bên dưới hoặc chat Zalo / gọi BookingCare" if dn
                       else "để lại thông tin qua nút đặt lịch hoặc nút Để lại thông tin tư vấn, hoặc chat Zalo / gọi BookingCare"),
                khoi=("KHỐI LIÊN HỆ ở cuối câu trả lời như thường lệ" if dn
                      else "các dòng sau, chép nguyên văn:\n" + KHOI_LIEN_HE_CA_NHAN)))
        if HOI_BHYT.search(t.nq) and now != "khong_doc_duoc":
            extra.append(self.ghi_chu("bhyt_kham_suc_khoe", cau=CAU_BHYT))
        if t.prev_situation in DA_HOI_LAI and now in ("goi_goi_y", "goi_tai_co_so", "giay_chua_co_o_tinh", "co_so_chua_hop_tac"):
            extra.append(self.ghi_chu("khong_hoi_lai"))
        # the customer named a package BookingCare does not have and no package is shown: collect the lead now (the
        # form) instead of only asking back, unless this turn already ends with a contact block
        if (t.unresolved and out.startswith("MÃ GÓI ĐÃ TRA: không có") and not now.startswith(("doanh_nghiep", "bv_cong"))
                and now != "khong_doc_duoc" and KHOI_LIEN_HE_CA_NHAN not in out + "".join(extra)):
            extra.append(self.ghi_chu("goi_khong_co_hien_form", ten="; ".join(f'"{u}"' for u in t.unresolved)))
        # driving licence at a place BookingCare does not partner with for it: that place's own reference package
        if t.purpose == "lai_xe" and self.goi_tk:
            names = ([self.co_so[f]["ten"] for f in getattr(t, "named_fac", None) or [] if f in self.co_so]
                     + [str(x) for x in t.need.get("co_so_nhac_toi") or []] + [str(x) for x in t.unresolved or []])
            shown = set(re.findall(r"GÓI THAM KHẢO (TK\d+)", out))
            named = [r for r in self.tham_khao_theo_ten(names) if r["ma"] not in shown]
            if named:
                extra.append(self.ghi_chu("co_so_lai_xe_tham_khao", ten=", ".join(dict.fromkeys(r["co_so"] for r in named))))
                out += "\n\n" + "\n\n".join(self.render_tham_khao(r) for r in named)
        if extra:
            head, _, body = out.partition("\n\n")
            out = "\n\n".join([head] + extra + [body])
        return out

    def ket_qua(self, tinh_huong, goi, co_so, parts):
        """What the answer step reads: one line with the ids looked up and the situation, then the parts."""
        head = (f"MÃ GÓI ĐÃ TRA: {', '.join(goi) or 'không có'} | MÃ CƠ SỞ ĐÃ TRA: {', '.join(co_so) or 'không có'}"
                f" | TÌNH HUỐNG: {tinh_huong}")
        return "\n\n".join([head] + [p for p in parts if p])

    def ld(self, key, **kw):
        """Instruction `key` of loi_dan.md, its {placeholders} filled from kw, else from the section of that name."""
        def fill(m):
            k = m.group(1)
            if k in kw:
                return str(kw[k])
            if k in LOI_DAN_GIA_TRI:
                return LOI_DAN_GIA_TRI[k]
            return self.ld(k, **kw)
        return re.sub(r"\{(\w+)\}", fill, self.loi_dan[key])

    def ghi_chu(self, key, **kw):
        return "GHI CHÚ: " + self.ld(key, **kw)

    def doc_yeu_cau(self, raw):
        """The request -> the turn (question, Dify user, packages of the previous answer, needs); None when the
        needs JSON cannot be read."""
        raw = raw or ""
        head, sep, question = raw.rpartition("\nHOI: ")
        raw, question = (head, question.strip()) if sep else (raw, "")
        # the chatflow sends "[<Dify user id>|<conversation id>] <question>" (older versions: "[<user>]")
        m = re.match(r"\[([^\]]*)\]\s*", question)
        user, question = (m.group(1), question[m.end():]) if m else ("", question)
        user, _, conversation = user.partition("|")
        prev, prev_situation = [], ""
        first, _, rest = raw.partition("\n")
        if first.startswith("TRUOC:"):
            prev = [x for x in re.findall(r"\d+", first) if x in self.goi]
            m = re.search(r"#([a-z_]+)", first)
            prev_situation = m.group(1) if m else ""
            raw = rest
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.S)  # reasoning models print their thoughts
        need = last_json_object(raw)
        if need is None:
            return None
        need["_nguoi_dung"] = user
        need["_hoi_thoai"] = conversation
        # old districts the customer names this turn or kept as the area ("Cầu Giấy", "quận 3"): packages there first
        area_text = " " + " ".join(re.findall(r"\w+", norm(f"{need.get('phuong_xa') or ''} {question}"))) + " "
        need["_quan"] = [a for areas in QUAN_CU.values() for a in areas if f" {a} " in area_text]
        if not province_name(need.get("tinh")):
            # "Hà Đông", "quận Tân Phú", "Thủ Đức": the old district tells the province
            area = province_of_area(str(need.get("phuong_xa") or "")) or province_of_area(question)
            if area:
                need = dict(need, tinh=area)
        return SimpleNamespace(need=need, question=question, nq=norm(question), prev=prev, prev_situation=prev_situation)

    def hieu(self, t):
        """Resolve what the turn refers to: facilities, packages, purpose, document types, place."""
        need, question, nq, prev = t.need, t.question, t.nq, t.prev
        # 1) facilities the customer names
        named_fac, unresolved, no_data, unknown_fac, which, both = [], [], [], [], [], []
        kept = need.get("_giu") or []  # fields the chatflow kept from earlier turns (he_thong/bot/gop_nhu_cau.py)
        new_province = province_name(need.get("tinh")) if "tinh" not in kept else ""
        ward = str(need.get("phuong_xa") or "").strip()
        if (ward and not need.get("co_so_nhac_toi") and not province_of_area(ward) and not province_in(ward)
                and not re.match(r"(phường|xã|quận|huyện|p\.|q\.)\s", ward, flags=re.I)
                and not any(norm(re.sub(r"^(phường|xã|p\.)\s*", "", ward, flags=re.I)) in phuong_cua_co_so(c.get("phuong_xa"))
                            for c in self.co_so.values())
                and self.find_co_so(ward)):
            # "khám cho nhân viên ở Thu Cúc": the place is a facility (system), not a ward
            need = t.need = dict(need, co_so_nhac_toi=[ward], phuong_xa="")
        asks_which_fac = bool(re.search(r"\b(co so|benh vien|phong kham|trung tam y te|vien)\s+nao\b", nq))
        if "co_so_nhac_toi" in kept and asks_which_fac:
            # "bệnh viện nào có gói...": asks which places have packages, not the kept facility
            need = t.need = dict(need, co_so_nhac_toi=[])
        if asks_which_fac and re.search(r"\bca nhan\b", nq):
            need = t.need = dict(need, muc_dich="tong_quat", ca_nhan=False, can=need.get("can") or ["danh_muc"])
        valid_mentions = []
        for mention in need.get("co_so_nhac_toi") or []:
            nm = norm(str(mention))
            if (re.fullmatch(r"\s*(nen tang\s+)?(bookingcare|booking care|bookingcae|bc)\s*", nm)
                    or re.search(r"\b(nao|khac|o dau)\b", nm)
                    or re.fullmatch(r"\s*(cac\s+|nhung\s+)?(co so|benh vien|phong kham|trung tam y te|vien)(\s+(y te|da khoa|cong|tu))?\s*", nm)):
                continue  # BookingCare itself or a generic question ("bệnh viện nào", "các trung tâm y tế") is not a facility name
            valid_mentions.append(mention)
            page = re.fullmatch(r"#(\d+)", str(mention).strip())
            hits = self.find_co_so(str(mention), tinh=province_name(need.get("tinh")))
            if hits and "co_so_nhac_toi" in kept and new_province:
                # a facility from earlier turns, and the customer now names a province: only if it is there
                hits = [h for h in hits if norm(self.co_so[h].get("tinh", "")) == norm(new_province)]
                if not hits:
                    continue
            if str(need.get("trang") or "") in hits and len(hits) > 1:
                hits = [str(need["trang"])]  # the page the customer is on decides between same-name places
            pub = [h for h in hits if self.co_so[h].get("loai") == "cong"]
            if len({self.co_so[h].get("tinh") for h in hits}) > 1 and province_name(need.get("tinh")):
                # the province the customer gave decides between same-name places
                hits = [h for h in hits if norm(self.co_so[h].get("tinh", "")) == norm(province_name(need.get("tinh")))] or hits
                pub = [h for h in hits if self.co_so[h].get("loai") == "cong"]
            if (len({self.co_so[h].get("tinh") for h in pub}) > 1 and not province_name(need.get("tinh"))
                    and len(need.get("co_so_nhac_toi") or []) == 1):
                if all(w in norm(question).split() for w in words(str(mention))):
                    which = pub  # "đại học y" in this very question: Hà Nội or TP.HCM?
                else:
                    both = pub  # asked already and the customer went on without choosing: answer for both
            named_fac += hits
            if not hits:
                # a facility on the list whose data is not ready yet (Bạch Mai): known, so the customer still
                # gets packages elsewhere in the same province instead of a dead end
                hidden = ([page.group(1)] if page.group(1) in self.an else []) if page else self.an_names.find(str(mention), 1)
                no_data += hidden
                if not hidden and not page:  # an unknown page id is no name the customer used
                    unresolved.append(str(mention))
                    unknown_fac.append(str(mention))
        need = t.need = dict(need, co_so_nhac_toi=valid_mentions)
        named_fac = list(dict.fromkeys(named_fac))
        t.no_data = list(dict.fromkeys(no_data))
        t.no_data_names = ", ".join(self.an[m]["ten"] for m in t.no_data)
        if t.no_data and not need.get("tinh"):
            need = dict(need, tinh=self.an[t.no_data[0]].get("tinh", ""))
        t.need_no_data = need
        # public hospitals: no packages there, the answer explains how to get a check-up (shown with the facilities)
        cong = [f for f in named_fac if self.co_so[f].get("loai") == "cong"]
        named_fac = [f for f in named_fac if f not in cong]
        # "bên mình chủ nhật có làm không", "có bảng giá khám không": about a facility without naming one -> the
        # facilities of the packages the previous answer showed
        # "Hồng Ngọc có những gói nào" asks for the package list even when the pick step marks it hoi_co_so
        asks_packages = re.search(r"(nhung|cac|danh sach|bao nhieu)\s+goi|goi\s+(kham\s+)?(nao|gi|o|tai)\b", nq)
        about_fac = need.get("hoi_co_so") and not asks_packages
        inferred_fac = []
        if about_fac and not named_fac and not need.get("goi_nhac_toi") and not unresolved and prev:
            inferred_fac = list(dict.fromkeys(self.goi[i]["ma_co_so"] for i in prev
                                              if self.goi[i].get("ma_co_so") in self.co_so))[:MAX_CO_SO]
            named_fac = inferred_fac

        # 2) packages the customer refers to: by position in the previous answer, or by what they called it;
        #    a name too vague for the whole catalog ("gói lái xe") is searched again among the packages of
        #    the facility the customer named ("gói lái xe ở An Việt")
        picked, ambiguous = [], []
        if need.get("cac_goi_truoc"):
            picked += prev
        for k in need.get("thu_tu") or []:
            if str(k).isdigit() and 1 <= int(k) <= len(prev):
                picked.append(prev[int(k) - 1])
        at_named = ({m for m, g in self.goi.items() if g.get("ma_co_so") in self.facility_family(named_fac)}
                    if named_fac else None)
        doc_words = [w for t in LOAI_GIAY if t in (need.get("cac_loai_giay") or []) + [need.get("muc_dich")]
                     for w in LOAI_GIAY_TU[t]]
        for mention in need.get("goi_nhac_toi") or []:
            if VAN_BAN.match(str(mention)):
                continue
            hits = [str(mention)] if str(mention) in self.goi else self.goi_names.find(str(mention))
            if not hits and at_named:
                hits = self.goi_names.find(str(mention), only=at_named)
            picked += hits
            if len(hits) > 1:
                ambiguous.append(str(mention))
            if not hits and any(re.search(rf"\b{w}\b", norm(str(mention))) for w in doc_words):
                continue  # "gói khám sức khoẻ xin việc theo bộ y tế" describes the document asked for, names no package
            if not hits and not cong:  # at a public hospital a package name is one of the hospital's own
                unresolved.append(str(mention))
        picked = list(dict.fromkeys(picked))

        # 3) purpose and document types
        criteria = any(need.get(k) for k in ("gioi_tinh", "tinh", "ngan_sach", "muc_dich", "tu_khoa"))
        if (need.get("muc_dich") == GIAY_TO_CHUA_RO and re.search(r"\bkham suc khoe\b", nq)
                and not re.search(r"\b(giay|ho so|thu tuc|chung nhan|bang lai|xin viec|nhap hoc)\b", nq)
                and not any(re.search(rf"\b{w}\b", nq) for ws in LOAI_GIAY_TU.values() for w in ws)):
            need = dict(need, muc_dich="", cac_loai_giay=[])  # "khám sức khỏe tại Bạch Mai" asks no certificate
        purpose = need.get("muc_dich") or ""
        if (purpose in ("", CHUA_RO_DOI_TUONG, "tong_quat") and TT_DOANH_NGHIEP.search(nq) and not TT_CA_NHAN.search(nq)
                and not picked and not need.get("trieu_chung") and not need.get("co_so_nhac_toi")):
            purpose = "doanh_nghiep"  # "TT25 giá sao", "gói khám thông tư 25": the company flow, not "cho ai?"
            need = dict(need, muc_dich=purpose)
        if purpose == "doanh_nghiep" and CA_NHAN_CHO_CTY.search(nq) and not DN_NHIEU_NGUOI.search(nq):
            purpose = "lai_xe" if re.search(r"\b(lai xe|bang lai)\b", nq) else "di_lam_di_hoc"
            need = dict(need, muc_dich=purpose)
        kinds = [k for k, ws in LOAI_GIAY_TU.items() if any(re.search(rf"\b{w}\b", nq) for w in ws)]
        if "lai_xe" in kinds and "di_lam_di_hoc" in kinds and re.search(r"lái\s+xe\s+đi\s+làm|lai\s+xe\s+di\s+lam", question, re.I):
            kinds = [k for k in kinds if k != "di_lam_di_hoc"]  # "lái xe đi làm" (working as a driver), not two documents
        if len(kinds) == 1 and purpose in ("", GIAY_TO_CHUA_RO):
            purpose = kinds[0]
            need = dict(need, muc_dich=purpose)
        if len(kinds) < 2 and purpose in {GIAY_TO_CHUA_RO, ""} | LOAI_GIAY:
            # this turn only adds a place ("hà nội"): the document types named in earlier turns still count
            earlier = [k for k in LOAI_GIAY_TU if k in (need.get("cac_loai_giay") or [])]
            if len(earlier) > 1:
                kinds = earlier
        # "xuất khẩu lao động" / JPETS is a document for the worker, not a company check-up ("người lao động")
        if purpose == "doanh_nghiep" and "di_nhat" in kinds and not re.search(r"cong ty|doanh nghiep|nhan vien|cbcnv", nq):
            purpose = need["muc_dich"] = "di_nhat"

        if DA_KET_HON.search(nq):  # "đã kết hôn", "có gia đình": no singles / pre-marriage package this turn
            need = dict(need, _da_ket_hon=True)
        if NGUOI_LON.search(nq) and not need.get("tuoi"):
            need = dict(need, _nguoi_lon=True)
        if KET_HON_NUOC_NGOAI.search(nq):  # "kết hôn với người Việt Nam" (a foreigner), "yếu tố nước ngoài"
            purpose = "ket_hon_nuoc_ngoai"
            need = dict(need, muc_dich=purpose)
        t.need, t.which, t.both, t.unknown_fac, t.unresolved = need, which, both, unknown_fac, unresolved
        t.named_fac, t.cong, t.about_fac, t.inferred_fac = named_fac, cong, about_fac, inferred_fac
        t.from_page = self.tu_trang(need)
        t.picked, t.ambiguous, t.criteria, t.purpose, t.kinds = picked, ambiguous, criteria, purpose, kinds
        t.where = province_name(need.get("tinh")) or province_in(nq)
        t.symptoms = need.get("trieu_chung")
        # the turn asks nothing but what is missing (a preparation / facility / package-detail question is
        # answered, not asked back)
        t.alone = (not picked and not named_fac and not cong and not unresolved and not t.no_data
                   and not need.get("can") and not need.get("hoi_co_so") and not HOI_CHUAN_BI.search(nq))

    def chon_tinh_huong(self, t):
        """The situation of this turn: the first rule that holds wins, so the order is the priority. Each name
        is answered by the method tl_<name>; its instructions are in loi_dan.md."""
        need = t.need
        free = not t.picked and not t.named_fac  # no package or private facility pointed at
        if t.which:
            return "hoi_benh_vien_nao"
        # two document types at once ("khám lấy giấy phép lái xe, đi làm tại Hồ Chí Minh")
        if len(t.kinds) > 1 and free and t.purpose != "doanh_nghiep":
            return "giay_nhieu_loai" if t.where else "giay_nhieu_loai_hoi_khu_vuc"
        # one question before either flow (a named public hospital answers itself)
        if t.purpose == CHUA_RO_DOI_TUONG and not t.picked and not t.cong:
            return "hoi_doi_tuong"
        wants_cong = need.get("bv_cong") or re.search(r"\b(benh vien|bv|vien) (cong|nha nuoc)\b", t.nq)
        if wants_cong and free and not t.cong and t.purpose not in {"doanh_nghiep"} | LOAI_GIAY:
            tinh = province_name(need.get("tinh"))
            if not tinh:
                return "bv_cong_hoi_tinh"
            return "bv_cong_danh_sach" if self.bv_cong_o(tinh) else "bv_cong_chua_co_o_tinh"
        if t.purpose == GIAY_TO_CHUA_RO and free:
            return "hoi_loai_giay"
        # the document type is known, the place is not: packages elsewhere are no use (a Hà Nội customer was
        # offered a TP.HCM "đi làm" package)
        if (t.purpose in LOAI_GIAY and not t.where and free and not t.cong and not t.unresolved and not t.no_data
                and not (need.get("hoi_co_so") and t.prev)  # a price question about nothing discussed yet
                and any(t.purpose in g["tags"] for g in self.goi.values())):  # none anywhere: say so, ask nothing
            return "giay_hoi_khu_vuc"
        if (t.alone and not t.symptoms and not t.purpose and not t.criteria and not need.get("tuoi")
                and not t.prev and re.search(r"\b(kham|goi)\b", t.nq)):
            return "hoi_nhu_cau"
        if (t.alone and not t.symptoms and not t.where and not t.prev and not need.get("tu_khoa")
                and (t.purpose or need.get("gioi_tinh") or need.get("tuoi") or need.get("ngan_sach"))
                and t.purpose in {"", "tong_quat", "ung_thu", "tien_hon_nhan", "tre_em", "chuyen_khoa"}):
            return "ca_nhan_hoi_khu_vuc"
        if t.no_data and not t.purpose and free and not t.cong and not need.get("tu_khoa"):
            return "co_so_chua_co_du_lieu"
        # a company enquiry (or a company package named) always gets the company answer: facilities on the
        # list, the Thông tư 25 checklist, the sign-up link; never a package price. ca_nhan is only asked for
        # alongside a company need, even when the pick step put the personal purpose in muc_dich
        if (t.purpose == "doanh_nghiep" or need.get("ca_nhan")
                or (t.picked and all("doanh_nghiep" in self.goi[i]["tags"] for i in t.picked))):
            return "doanh_nghiep"
        if t.unknown_fac and free and not t.cong and not t.no_data and not need.get("trieu_chung"):
            return "co_so_chua_hop_tac" if t.where else "co_so_chua_hop_tac_hoi_khu_vuc"
        return "tra_goi"

    # ---------- the answer of each situation
    def tu_trang(self, need):
        """The facilities the customer never named: they come from the page the chat is on ("#<id>")."""
        return [m[1:] for m in map(str, need.get("co_so_nhac_toi") or []) if re.fullmatch(r"#\d+", m) and m[1:] in self.co_so]

    def trang_note(self, ids):
        return self.ghi_chu("trang_dang_xem", ten=", ".join(self.co_so[f]["ten"] for f in ids)) if ids else ""

    def no_data_note(self, t, goi_khac=True):
        note = self.ghi_chu("co_so_chua_co_du_lieu", ten=t.no_data_names)
        if goi_khac:
            note += " " + self.ld("co_so_chua_co_du_lieu.goi_khac",
                                  tinh=province_name(t.need.get("tinh")) or "khu vực khách hỏi")
        return note

    def tl_hoi_benh_vien_nao(self, t):
        names = "; ".join(f"{self.co_so[h]['ten']} ({self.co_so[h].get('tinh', '')})" for h in t.which)
        return self.ket_qua("hoi_benh_vien_nao", [], [], [self.ghi_chu("hoi_benh_vien_nao", ten=names)])

    def tl_giay_nhieu_loai_hoi_khu_vuc(self, t):
        has_ellipsis = "..." in t.question or "…" in t.question
        kinds_list = [LOAI_GIAY_TEN[k] for k in t.kinds]
        kinds = ", ".join(kinds_list) + (", ..." if has_ellipsis else "")
        blocks = self.khoi_luu_y_giay(t.kinds)
        parts = [self.ghi_chu("giay_nhieu_loai_hoi_khu_vuc", loai_giay=kinds)]
        if blocks:  # "dùng giấy khám xin việc làm bằng lái được không?": answered from the notes before asking
            parts.append(self.ghi_chu("giay_nhieu_loai.luu_y"))
        return self.ket_qua("giay_nhieu_loai_hoi_khu_vuc", [], [], parts + blocks)

    def khoi_luu_y_giay(self, kinds):
        """The general notes block (kien_thuc/giay_to/<loai>.md) of each document type that has one."""
        return ["=== LƯU Ý CHUNG: GIẤY KHÁM " + LOAI_GIAY_TEN[k].upper() + " ===\n"
                + "\n\n".join(f"{h}:\n{txt}" for h, txt in self.luu_y_giay[k]) for k in kinds if self.luu_y_giay.get(k)]

    def tl_giay_nhieu_loai(self, t):
        """The packages of each document type in the province the customer gave."""
        ids, missing = [], []
        for k in t.kinds:
            found = self.loc(dict(t.need, muc_dich=k, tinh=t.where, tu_khoa=[]))[:3]
            ids += found
            if not found:
                missing.append(LOAI_GIAY_TEN[k])
                log_missing("khong_co_goi_o_tinh", dict(t.need, muc_dich=k, tinh=t.where), t.question, LOAI_GIAY_TEN[k])
        thieu = " " + self.ld("giay_nhieu_loai.thieu", thieu_giay=", ".join(missing), tinh=t.where) if missing else ""
        note = self.ghi_chu("giay_nhieu_loai", loai_giay="; ".join(LOAI_GIAY_TEN[k] for k in t.kinds), tinh=t.where,
                            thieu=thieu)
        blocks = self.khoi_luu_y_giay(t.kinds)
        extra = [self.ghi_chu("giay_nhieu_loai.luu_y")] if blocks else []
        return self.ket_qua("giay_nhieu_loai", ids, [], [note] + extra + [self.render_goi(self.goi[i], []) for i in ids] + blocks)

    def tl_hoi_doi_tuong(self, t):
        # ask everything the next step needs in one question: who it is for and, if unknown, where
        where = province_name(t.need.get("tinh")) or str(t.need.get("phuong_xa") or "")
        khu_vuc = "" if where else self.ld("hoi_doi_tuong.khu_vuc")
        return self.ket_qua("hoi_doi_tuong", [], [], [self.ghi_chu("hoi_doi_tuong", khu_vuc=khu_vuc)])

    def bv_cong_co_du_lieu(self):
        return [m for m, c in self.co_so.items() if c.get("loai") == "cong" and any(v for _, v in c.get("ktq", []))]

    def bv_cong_o(self, tinh):
        """The public hospitals with check-up data in the province (US-02 covers only hospitals with data)."""
        return [m for m in self.bv_cong_co_du_lieu() if norm(self.co_so[m].get("tinh", "")) == norm(tinh)]

    def tl_bv_cong_hoi_tinh(self, t):
        provinces = ", ".join(dict.fromkeys(self.co_so[m]["tinh"] for m in self.bv_cong_co_du_lieu()))
        return self.ket_qua("bv_cong_hoi_tinh", [], [], [self.ghi_chu("bv_cong_hoi_tinh", cac_tinh=provinces)])

    def tl_bv_cong_chua_co_o_tinh(self, t):
        tinh = province_name(t.need.get("tinh"))
        return self.ket_qua("bv_cong_chua_co_o_tinh", [], [], [self.ghi_chu("bv_cong_chua_co_o_tinh", tinh=tinh)])

    def tl_bv_cong_danh_sach(self, t):
        """'Tôi muốn khám bệnh viện công' with no hospital named: the public hospitals of the province; ask which."""
        tinh = province_name(t.need.get("tinh"))
        here = self.bv_cong_o(tinh)[:MAX_CO_SO_DN]
        lines = ["=== BỆNH VIỆN CÔNG CÓ HƯỚNG DẪN KHÁM TỔNG QUÁT ==="]
        for m in here:
            c, ktq = self.co_so[m], dict(self.co_so[m]["ktq"])
            first = re.split(r"(?<=[.!?])\s", re.sub(r"\s+", " ", ktq.get("Hình thức khám tổng quát") or "").strip())[0][:300]
            book = (ktq.get("Cách đặt lịch") or "").replace("\n", " ")  # whole: a cut could split a phone number
            lines.append(f"- {c['ten']} | Địa chỉ: {c.get('dia_chi') or 'KHÔNG CÓ TRONG DỮ LIỆU'}"
                         f" | Hình thức: {first or 'KHÔNG CÓ TRONG DỮ LIỆU'} | Đặt lịch: {book or c.get('dien_thoai') or 'KHÔNG CÓ TRONG DỮ LIỆU'}")
        return self.ket_qua("bv_cong_danh_sach", [], here, [self.ghi_chu("bv_cong_danh_sach", tinh=tinh), "\n".join(lines)])

    def tl_hoi_loai_giay(self, t):
        kinds = "; ".join(LOAI_GIAY_TEN[k] for k in sorted(LOAI_GIAY, key=list(LOAI_GIAY_TEN).index))
        khu_vuc = "" if province_name(t.need.get("tinh")) else " " + self.ld("hoi_loai_giay.khu_vuc")
        return self.ket_qua("hoi_loai_giay", [], [], [self.no_data_note(t, goi_khac=False) if t.no_data else "",
                                                      self.ghi_chu("hoi_loai_giay", loai_giay=kinds, khu_vuc=khu_vuc)])

    def tl_giay_hoi_khu_vuc(self, t):
        # a general question about the document ("cần khám những gì") is answered from its shared notes first
        notes = self.luu_y_giay.get(t.purpose) or []
        block = ("=== LƯU Ý CHUNG: GIẤY KHÁM " + LOAI_GIAY_TEN[t.purpose].upper() + " ===\n" +
                 "\n\n".join(f"{h}:\n{txt}" for h, txt in notes)) if notes else ""
        tra_loi_chung = self.ld("giay_hoi_khu_vuc.co_luu_y") + " " if block else ""
        if t.purpose == "lai_xe":
            # driving licence (version 1): every missing detail is asked at once, each with choices from the data
            goi = [g for g in self.goi.values() if "lai_xe" in g["tags"]]
            dem = collections.Counter(g["tinh"] for g in goi if g["tinh"])
            tinh = [p.replace("Thành phố ", "TP. ") for p in sorted(dem, key=lambda p: (-dem[p], p != "Hà Nội"))]
            thieu = [f"- **Khu vực:** {', '.join(tinh)} hay tỉnh/thành khác (có thể kèm quận/phường)?"]
            if not t.need.get("hang_bang"):
                thieu.append("- **Hạng bằng:** A1, A (xe máy), B (ô tô) hay C1, C, D... (xe tải, xe khách)?")
            if not (t.need.get("ngan_sach") or t.need.get("ngan_sach_tu")):
                gia = [g["gia_tu"] for g in goi if g["gia_tu"]]
                muc = [(0, 500000, "dưới 500.000đ"), (500000, 1000000, "500.000đ – 1.000.000đ"), (1000000, 0, "trên 1.000.000đ")]
                co = [ten for lo, hi, ten in muc if any(p >= lo and (not hi or p < hi) for p in gia)]
                thieu.append(f"- **Khoảng giá:** {' hay '.join(co) if len(co) <= 2 else ', '.join(co[:-1]) + ' hay ' + co[-1]}?")
            note = self.ghi_chu("giay_hoi_khu_vuc.lai_xe", tra_loi_chung=tra_loi_chung, thieu="\n".join(thieu))
        else:
            note = self.ghi_chu("giay_hoi_khu_vuc", ten_giay=LOAI_GIAY_TEN[t.purpose], tra_loi_chung=tra_loi_chung)
        return self.ket_qua("giay_hoi_khu_vuc", [], [], [note, block])

    def tl_hoi_nhu_cau(self, t):
        return self.ket_qua("hoi_nhu_cau", [], [], [self.ghi_chu("hoi_nhu_cau")])

    def tl_ca_nhan_hoi_khu_vuc(self, t):
        # one question for the place plus whatever else is missing
        missing = ["khu vực hoặc cơ sở muốn khám"]
        if t.purpose != "tre_em":
            if not t.need.get("gioi_tinh"):
                missing.append("giới tính")
            if not t.need.get("tuoi"):
                missing.append("độ tuổi")
        return self.ket_qua("ca_nhan_hoi_khu_vuc", [], [], [self.ghi_chu("ca_nhan_hoi_khu_vuc", can_hoi=", ".join(missing))])

    def tl_co_so_chua_co_du_lieu(self, t):
        tinh = province_name(t.need.get("tinh")) or "khu vực của khách"
        return self.ket_qua("co_so_chua_co_du_lieu", [], [], [
            self.no_data_note(t, goi_khac=False) + " " + self.ld("co_so_chua_co_du_lieu.hoi_nhu_cau", tinh=tinh)])

    def tl_doanh_nghiep(self, t):
        # also asking for themselves / family: personal packages they named, else suggestions by their criteria
        # (at the facility they named, if any)
        need = t.need
        personal = [i for i in t.picked if "doanh_nghiep" not in self.goi[i]["tags"]] if need.get("ca_nhan") else []
        if need.get("ca_nhan") and not personal and not t.cong:  # at a public hospital: its own check-up data
            at = self.facility_family(t.named_fac) if t.named_fac else None
            own = t.purpose if t.purpose not in ("doanh_nghiep", "") else ("" if need.get("tu_khoa") else "tong_quat")
            personal = self.loc(dict(need, muc_dich=own), at=at)[:MAX_GOI]
        return self.render_doanh_nghiep(need, t.question, personal, ca_nhan=bool(need.get("ca_nhan")),
                                        prev_situation=t.prev_situation)

    def tl_co_so_chua_hop_tac_hoi_khu_vuc(self, t):
        names = ", ".join(f'"{u}"' for u in t.unknown_fac)
        return self.ket_qua("co_so_chua_hop_tac_hoi_khu_vuc", [], [], [self.ghi_chu("co_so_chua_hop_tac_hoi_khu_vuc", ten=names)])

    def tl_co_so_chua_hop_tac(self, t):
        """A facility the data does not know, in a known province: one partner package per facility there."""
        names = ", ".join(f'"{u}"' for u in t.unknown_fac)
        own = t.purpose or ("" if t.need.get("tu_khoa") else "tong_quat")
        alt = self.loc(dict(t.need, tinh=t.where, muc_dich=own))
        alt = [i for k, i in enumerate(alt) if self.goi[i]["ma_co_so"] not in
               {self.goi[j]["ma_co_so"] for j in alt[:k]}][:MAX_CO_SO]  # one package per facility
        log_missing("co_so_chua_hop_tac", t.need, t.question, names)
        if alt:
            ten, note = "co_so_chua_hop_tac", self.ghi_chu("co_so_chua_hop_tac", ten=names, tinh=t.where)
        else:
            ten, note = "co_so_chua_hop_tac_khong_co_goi", self.ghi_chu("co_so_chua_hop_tac_khong_co_goi", ten=names, tinh=t.where)
        return self.ket_qua(ten, alt, [], [note] + [self.render_goi(self.goi[i], []) for i in alt])

    def tl_tra_goi(self, t):
        """The packages the customer points at, else the packages AT the named facilities (filtered by any other
        criteria), else suggestions by the customer's criteria; plus the facility details when the question is
        about a facility. Reported under a more precise name (goi_khach_chon, goi_tai_co_so, goi_goi_y...)."""
        need, question, prev = t.need, t.question, t.prev
        picked, named_fac, cong, purpose = t.picked, t.named_fac, t.cong, t.purpose
        note, note_key, suggested, listing = "", "", [], False
        tk = []  # reference driving-licence packages (non-partner facilities) filling up the list

        def loc_items(at):
            """loc(), but when no package has the asked item ('lấy máu'), the packages without it, with a note:
            the customer often asks whether a package includes something, and 'no' is the answer."""
            found = self.loc(need, at=at)
            if found or not need.get("tu_khoa"):
                return found, ""
            found = self.loc(dict(need, tu_khoa=[]), at=at)
            items = ", ".join(f'"{k}"' for k in need["tu_khoa"])
            return found, self.ghi_chu("khong_khop_hang_muc", cum=items) if found else ""

        if not picked and named_fac and (not t.about_fac or purpose in LOAI_GIAY):
            listing = not t.about_fac
            at = self.facility_family(named_fac)
            suggested, note = loc_items(at)
            # criteria kept from earlier turns (budget, purpose) may rule out every package at the place the
            # customer now names: show what the place has and say how it differs, instead of "no data"
            if not suggested and need.get("ngan_sach"):
                suggested = self.loc(dict(need, ngan_sach=0), at=at)
                if suggested:
                    note = self.ghi_chu("co_so_vuot_ngan_sach", ngan_sach=money(int(need["ngan_sach"])))
            if not suggested and purpose in LOAI_GIAY:
                # a document package is asked for at a place that has none: never its other packages instead
                ten = LOAI_GIAY_TEN[purpose]
                fac_prov = self.co_so[named_fac[0]].get("tinh", "")
                suggested = self.loc(dict(need, tinh=fac_prov, ngan_sach=0))[:MAX_CO_SO]
                names = ", ".join(self.co_so[f]["ten"] for f in named_fac)
                tiep = (self.ld("co_so_khong_co_goi_giay.goi_khac", ten_giay=ten, tinh=fac_prov) if suggested
                        else self.ld("thu_lead"))
                note_key, note = "co_so_khong_co_goi_giay", self.ghi_chu("co_so_khong_co_goi_giay", ten=names,
                                                                         ten_giay=ten, tiep=tiep)
                log_missing("khong_co_goi_loai_giay", need, question, f"{ten} tại {names}")
                listing = False
            if not suggested and purpose not in LOAI_GIAY:
                suggested = self.loc(dict(need, ngan_sach=0, muc_dich="", tu_khoa=[]), at=at)
                if suggested:
                    note = self.ghi_chu("co_so_khong_dung_dieu_kien")
            if not suggested and not note:
                note = self.ghi_chu("co_so_khong_co_goi")
        elif t.criteria and not picked and not t.unresolved and not cong and not (t.about_fac and not named_fac and not t.where and purpose not in LOAI_GIAY):
            # a named facility limits the suggestions to that place (and its branches)
            at = self.facility_family(named_fac) if named_fac else None
            suggested, note = loc_items(at)
            if purpose == "lai_xe" and at is None:
                # partner packages first; when they are fewer than the list holds, reference places fill it up
                tk = self.loc_tham_khao(need)[:max(0, MAX_GOI - len(suggested))]
            if tk and not suggested:
                note_key, note = "lai_xe_chi_tham_khao", self.ghi_chu(
                    "lai_xe_chi_tham_khao", tinh=province_name(need.get("tinh")),
                    khoang=(" có giá " + khoang_gia(need.get("ngan_sach_tu"), need.get("ngan_sach"))
                            if need.get("ngan_sach_tu") or need.get("ngan_sach") else ""))
            elif not suggested and need.get("ngan_sach_tu"):
                # nothing in the range: the packages closest to it (bounds loosened by 30%)
                suggested = self.loc(need, budget_factor=1.3, at=at)
                if suggested:
                    note = self.ghi_chu("ngoai_khoang_gia", khoang=khoang_gia(need.get("ngan_sach_tu"), need.get("ngan_sach")))
            elif not suggested and need.get("ngan_sach"):
                suggested = self.loc(need, budget_factor=1.3, at=at)
                if suggested:
                    note = self.ghi_chu("vuot_ngan_sach")
            if not suggested and not tk and purpose in LOAI_GIAY:
                # no package for this document type in the customer's area: say so; packages in another
                # province are not offered (the customer chose where to go), only the BookingCare contacts
                ten, where = LOAI_GIAY_TEN[purpose], province_name(need.get("tinh"))
                elsewhere = self.loc(dict(need, tinh="", ngan_sach=0), at=at) if where else []
                if elsewhere:
                    places = ", ".join(dict.fromkeys(self.goi[i]["tinh"] for i in elsewhere if self.goi[i]["tinh"]))
                    note_key, note = "giay_chua_co_o_tinh", self.ghi_chu("giay_chua_co_o_tinh", ten_giay=ten, tinh=where,
                                                                         noi_khac=places)
                    log_missing("khong_co_goi_o_tinh", need, question, ten)
                else:
                    note_key, note = "giay_chua_co", self.ghi_chu("giay_chua_co", ten_giay=ten)
                    log_missing("khong_co_goi_loai_giay", need, question, ten)
        if cong and purpose in LOAI_GIAY and not picked:
            # a document check-up at a public hospital: the hospital's own guidance only, no partner packages
            note_key = "bv_cong_giay"
        limit = MAX_LIST if listing else MAX_GOI
        ids = (picked + suggested)[:limit]
        symptoms, inferred_fac = need.get("trieu_chung"), t.inferred_fac
        if symptoms and not picked:  # a symptom is for a doctor: no facility or package suggestions
            ids, named_fac, inferred_fac, listing, note = [], [], [], False, ""
        if listing and len(suggested) > limit:
            note = self.ghi_chu("co_so_nhieu_goi", so_goi=len(suggested), gioi_han=limit)

        # facility details: the named ones, plus those of the packages, when the question is about a facility
        fac = list(named_fac) if t.about_fac or not listing else []
        if t.about_fac and (picked or named_fac):
            fac += [self.goi[i]["ma_co_so"] for i in ids if self.goi[i].get("ma_co_so") in self.co_so]
        fac = list(dict.fromkeys(cong + fac))[:MAX_CO_SO]

        phan = [p for p in need.get("can") or [] if p in GOI_PHAN]
        if "luu_y_giay" in phan and "chuan_bi" not in phan:  # "giấy tờ cần mang" is often in the preparation text
            phan.append("chuan_bi")
        if not phan:  # listing many packages: basic info only (name, place, price, schedule, link)
            phan = [] if listing and len(ids) > MAX_GOI else list(GOI_PHAN)
        parts = [self.trang_note(t.from_page)]
        if listing:
            parts.append(self.ghi_chu("liet_ke_tai_co_so", ten=", ".join(self.co_so[f]["ten"] for f in named_fac)))
        if t.no_data:
            parts.append(self.no_data_note(t))
        if t.both:
            parts.append(self.ghi_chu("hai_benh_vien", ten=" và ".join(self.co_so[h]["ten"] for h in t.both)))
        shown_before = [i for i in prev if i not in ids]
        if shown_before:
            # the packages of the previous answer, so a follow-up turn ("cụ thể đi") does not take them for made up
            parts.append(self.ld("khoi_goi_luot_truoc") + "\n" + "\n".join(
                f"- {self.goi[i]['ten']} | {self.goi[i].get('noi_kham') or (self.co_so.get(self.goi[i].get('ma_co_so'), {}) or {}).get('ten', '')}"
                f" | Giá: {self.gia(self.goi[i]) or 'chưa có'}" for i in shown_before[:8]))
        if t.unresolved:
            parts.append(self.ghi_chu("khong_tim_thay", ten="; ".join(f'"{u}"' for u in t.unresolved)))
            if not ids:  # nothing else to show: collect the lead instead of only saying there is no information
                parts.append(self.ld("thu_lead"))
        if (need.get("thu_tu") or need.get("cac_goi_truoc")) and not prev:
            parts.append(self.ghi_chu("khong_ro_goi_truoc"))
        if t.criteria and not ids and not t.unresolved and not symptoms and not cong and not note and not (t.about_fac and not fac):
            parts.append(self.ghi_chu("khong_co_goi_phu_hop"))
        if t.ambiguous:
            parts.append(self.ghi_chu("nhieu_goi_khop", ten="; ".join(f'"{a}"' for a in t.ambiguous)))
        if inferred_fac:
            parts.append(self.ghi_chu("co_so_luot_truoc", ten=", ".join(self.co_so[f]["ten"] for f in inferred_fac)))
        elif need.get("hoi_co_so") and not fac and not symptoms:
            parts.append(self.ghi_chu("hoi_co_so_chua_ro"))
        if cong and not symptoms:
            parts.append(self.ghi_chu("bv_cong", ten=", ".join(self.co_so[f]["ten"] for f in cong),
                                      loai_kham=loai_kham(purpose, question)))
        if HOI_KHAM_LE.search(norm(question)) and not symptoms and not cong:
            parts.append(self.ghi_chu("kham_le"))
        if need.get("trieu_chung"):
            known = province_name(need.get("tinh"))
            parts.append(self.ghi_chu("trieu_chung", trieu_chung="; ".join(map(str, need["trieu_chung"])),
                                      hoi_them=(f"khách đã cho biết khu vực ({known}): KHÔNG hỏi lại khu vực; khách cần thì cho biết nhu cầu để được gợi ý gói"
                                                if known else "khách cần thì cho biết khu vực và nhu cầu để được gợi ý gói")))
        if note:
            parts.append(note)
        if (suggested or tk) and not listing and not named_fac:
            ward = norm(re.sub(r"^(phường|xã|p\.)\s*", "", need.get("phuong_xa") or "", flags=re.I))
            quan = need.get("_quan") or []
            if ward or quan:
                khu = str(need.get("phuong_xa") or "") or ", ".join(quan)
                in_area = [self.goi[i]["ten"] for i in ids if self.o_khu_vuc(self.goi[i], ward, quan)]
                in_area += [f"{r['ten_goi']} tại {r['co_so']} (tham khảo)" for r in tk  # reference places count too
                            if (ward and f" {ward} " in r["_dc"]) or any(f" {a} " in r["_dc"] for a in quan)]
                parts.append(self.ghi_chu("khu_vuc_co_goi", khu=khu, ten="; ".join(in_area))
                             if in_area else self.ghi_chu("khu_vuc_chua_co_goi", khu=khu,
                                                          tinh=province_name(need.get("tinh")) or "tỉnh/thành này"))
            if (need.get("ngan_sach_tu") or need.get("ngan_sach")) and not note and ids:
                # a price range: the packages in the area first, then from the lowest price, in the order given
                w, q = ward, need.get("_quan") or []
                ids = sorted(ids, key=lambda i: (not self.o_khu_vuc(self.goi[i], w, q), self.goi[i]["gia_tu"]))
                parts.append(self.ghi_chu("khoang_gia", khoang=khoang_gia(need.get("ngan_sach_tu"), need.get("ngan_sach")),
                                          thu_tu="; ".join(f"{self.goi[i]['ten']} ({self.gia(self.goi[i])})" for i in ids)))
        if purpose == "lai_xe" and need.get("hang_bang") and ids:
            parts.append(self.ghi_chu("hang_bang", hang=str(need["hang_bang"])))
        parts += [self.render_goi(self.goi[i], phan) for i in ids]
        if tk:
            if ids:
                parts.append(self.ghi_chu("lai_xe_them_tham_khao"))
            parts += [self.render_tham_khao(r) for r in tk]
        parts += [self.render_co_so(self.co_so[f]) for f in fac]
        if HOI_TT25.search(question) and self.render_tt25():
            parts.append(self.render_tt25())
        if HOI_CHUAN_BI.search(norm(question)) and self.hoi_dap:
            parts.append("=== HỎI ĐÁP CHUNG VỀ CHUẨN BỊ ĐI KHÁM (tổng hợp từ các gói trên BookingCare) ===\n"
                         + "\n\n".join(f"{h}?\n{txt}" for h, txt in self.hoi_dap))

        if symptoms and not picked:
            ten = "trieu_chung"
        elif note_key:
            ten = note_key
        elif cong:
            ten = "bv_cong"
        elif picked:
            ten = "goi_khach_chon"
        elif t.unresolved and not ids:
            ten = "khong_tim_thay"
        elif t.about_fac and fac:
            ten = "thong_tin_co_so"
        elif need.get("hoi_co_so") and not fac:
            ten = "hoi_co_so_chua_ro"
        elif listing:
            ten = "goi_tai_co_so"
        elif ids:
            ten = "goi_goi_y"
        elif t.criteria:
            ten = "khong_co_goi_phu_hop"
        else:
            ten = "khac"
        return self.ket_qua(ten, ids, fac, parts)

    def render_tt25(self):
        t = self.tt25
        if not t["sections"]:
            return ""
        out = ["=== DANH MỤC KHÁM THEO THÔNG TƯ 25/2026/TT-BYT ===", t["tieu_de"],
               "", "BẢN TRÌNH BÀY CHO KHÁCH (chép nguyên văn khi khách hỏi danh mục khám, không đổi, không thêm bớt):"]
        for n, (h, txt) in enumerate(t["sections"]):
            if n:  # a blank line between groups, or markdown joins them into one paragraph
                out.append("")
            items = [x.strip()[1:].strip() for x in txt.split("\n") if x.strip().startswith("-")]
            if items and all(":" not in x and len(x) <= 30 for x in items):  # short items: one line
                out.append(f"**{h}:** {', '.join(items)}")
                continue
            if len(items) == 1 and ":" not in items[0]:
                out.append(f"**{h}:** {items[0]}")
                continue
            out += [f"**{h}:**", ""]
            for x in items or [txt.strip()]:
                name, _, sub = x.partition(":")
                out.append(f"- **{name.strip()}:** {sub.strip()}" if sub.strip() else f"- {x}")
        return "\n".join(out)

    def doanh_nghiep_cho(self, mentions):
        """Company enquiry: which named places are on the list, which are not, and the province to suggest."""
        found, outside, cong, province = [], [], [], ""
        for mention in mentions:
            # the facility itself first: a loose match on the company list alone took "Đại học Y Hà Nội" for
            # "Đại học Quốc Gia Hà Nội"; a branch ('Hồng Ngọc Yên Ninh') of a listed system counts as the system
            named = self.find_co_so(str(mention))
            if not named and re.fullmatch(r"#\d+", str(mention).strip()):
                continue  # the page's facility, not on the list and not a name the customer used
            if named:
                fam = set()
                for f in named:
                    fam |= {f, self.co_so[f].get("thuoc_he_thong", "")}
                hits = [i for i, r in enumerate(self.doanh_nghiep) if r.get("ma_co_so") and r["ma_co_so"] in fam]
            else:  # names used only on the company list ("DYM Medical Center (Hà Nội)")
                hits = self.dn_names.find(str(mention))
            said = province_in(str(mention)) or province_of_area(str(mention))
            if hits and said:  # "Hoàn Mỹ Sài Gòn" is not the Hoàn Mỹ in Vinh
                hits = [i for i in hits if norm(self.doanh_nghiep[i]["tinh"]) == norm(said)]
            if hits:
                found += hits
            else:
                fac = self.find_co_so(str(mention), limit=1)
                if fac and self.co_so[fac[0]].get("loai") == "cong":
                    cong.append(fac[0])
                else:
                    outside.append(self.co_so[fac[0]]["ten"] if fac else str(mention))
                province = province or said or (self.co_so[fac[0]].get("tinh", "") if fac else "")
        return list(dict.fromkeys(found)), outside, list(dict.fromkeys(cong)), province

    @staticmethod
    def dn_in_ward(r, ward):
        """The facility is in the ward the customer named (its phuong_xa, or the ward written in its address:
        a system lists all its branches there)."""
        w = norm(re.sub(r"^(phường|phuong|xã|xa|p\.|x\.)\s+", "", ward, flags=re.I))
        fac = r.get("fac") or {}
        return bool(w) and (w in phuong_cua_co_so(fac.get("phuong_xa")) or re.search(rf"\b{re.escape(w)}\b", norm(fac.get("dia_chi", ""))))

    @staticmethod
    def the_manh(fac, limit=6):
        """The first items of the facility's 'Thế mạnh chuyên môn' section, as one line."""
        for h, t in (fac or {}).get("sections", []):
            if "the manh" in norm(h):
                items = [x.strip(" -•*").rstrip(".;") for x in t.split("\n") if x.strip().startswith(("-", "•", "*"))]
                # specialty names only ("Tim mạch"); long sentences (MEDLATEC's home sampling) made the answer run on
                return ", ".join([x for x in items if len(x) <= 60][:limit])
        return ""

    def chon_co_so_doanh_nghiep(self, rows, need, nq, ward, quan):
        """The company facilities to suggest, at most MAX_CO_SO_DN. Points only for what the customer asked for:
        cùng phường 40 / cùng quận cũ 25 (the customer gave the place), khớp thế mạnh 20 (named a specialty or
        symptom), khớp hóa đơn / đẩy thông tin 15 (asked for it), có giá 10 (asked about price). Equal scores: at
        random; when the customer asked about price, the lower known price first, a facility with no price last."""
        words = [norm(w) for w in (need.get("tu_khoa") or []) + (need.get("trieu_chung") or []) if norm(w)]
        hoi_hd, hoi_dt = bool(DN_HOI_HOA_DON.search(nq)), bool(DN_HOI_DAY_THONG_TIN.search(nq))
        hoi_gia = bool(DN_HOI_GIA.search(nq))
        cham = []
        for i, r in rows:
            fac = r.get("fac") or {}
            diem = 0
            if ward and self.dn_in_ward(r, ward):
                diem += 40
            elif quan and fac and any(f" {a} " in f" {norm(fac.get('dia_chi', ''))} " for a in quan):
                diem += 25
            if words and any(w in norm(self.the_manh(fac)) for w in words):
                diem += 20
            if (hoi_hd and r.get("xuat_hoa_don")) or (hoi_dt and r.get("day_thong_tin")):
                diem += 15
            key = (-diem,)
            if hoi_gia:
                co_gia = bool(r["gia_tu"] or r.get("khoang_gia"))
                diem += 10 if co_gia else 0
                key = (-diem, 0 if co_gia else 1, (r["gia_tu"] or 10**12) if co_gia else 0)
            cham.append((key, random.random(), i, r, diem))
        cham.sort(key=lambda x: (x[0], x[1]))
        chon = cham[:MAX_CO_SO_DN]
        self.ghi_nhat_ky_chon(cham, chon, need, ward)
        return [(i, r) for _, _, i, r, _ in chon]

    @staticmethod
    def ghi_nhat_ky_chon(cham, chon, need, ward):
        """nhat_ky/chon_co_so_dn.csv: every candidate with its score, the chosen ones, and the candidates tied on
        score and price (the ones the random pick could decide between)."""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            path = os.path.join(LOG_DIR, "chon_co_so_dn.csv")
            new = not os.path.exists(path)
            nhom = {}
            for key, _, _, r, _ in cham:
                nhom.setdefault(key, []).append(r["ten"])
            cung = [r["ten"] for key, _, _, r, _ in cham if len(nhom[key]) > 1]
            with _log_lock, open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["thoi_gian", "tinh", "phuong_xa", "ung_vien_diem", "chon", "cung_diem_va_gia"])
                w.writerow([datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S"), province_name(need.get("tinh")), ward,
                            " | ".join(f"{r['ten']}={diem}" for _, _, _, r, diem in cham),
                            " | ".join(r["ten"] for _, _, _, r, _ in chon), " | ".join(cung)])
        except OSError:  # never fail the answer because of the log
            pass

    def render_doanh_nghiep(self, need, question="", personal=(), ca_nhan=False, prev_situation=""):
        found, outside, cong, fac_province = self.doanh_nghiep_cho(need.get("co_so_nhac_toi") or [])
        province = province_name(need.get("tinh")) or fac_province
        priced = any(r["gia_tu"] or r.get("khoang_gia") for r in self.doanh_nghiep)
        notes = [self.ghi_chu("dn_chung", gia=self.ld("dn_gia_co" if priced else "dn_gia_chua_co"))]
        if self.tu_trang(need):
            notes.append(self.trang_note(self.tu_trang(need)))
        if personal:
            notes.append(self.ghi_chu("dn_ca_nhan"))
        if ca_nhan and cong and not personal:
            notes.append(self.ghi_chu("dn_ca_nhan_bv_cong", ten=", ".join(self.co_so[f]["ten"] for f in cong)))
        rows = list(enumerate(self.doanh_nghiep))
        if outside:
            notes.append(self.ghi_chu("dn_ngoai_danh_sach", ten="; ".join(outside)))
        if cong:
            notes.append(self.ghi_chu("dn_bv_cong", ten=", ".join(self.co_so[f]["ten"] for f in cong)))
        m = re.search(r"\b(\d{1,5})\s*(?:nguoi|nhan vien|nhan su|ns|cbcnv|lao dong)\b", norm(question))
        heads = int(m.group(1)) if m else 0
        ward = str(need.get("phuong_xa") or "").strip()
        tinh_huong = "doanh_nghiep"
        quan = [a for areas in QUAN_CU.values() for a in areas if f" {a} " in f" {norm(question)} "]  # old districts named
        if found:
            rows = [(i, self.doanh_nghiep[i]) for i in found]
        elif not province and not ward and prev_situation in ("doanh_nghiep_hoi_khu_vuc", "doanh_nghiep_chua_ro_khu_vuc"):
            # asked for the area last turn and still none: no third question, the places in the big cities
            by_city = [(i, r) for c in ("Hà Nội", "Thành phố Hồ Chí Minh") for i, r in rows if norm(r["tinh"]) == norm(c)]
            rows = [x for c in ("Hà Nội", "Thành phố Hồ Chí Minh") for x in [y for y in by_city if norm(y[1]["tinh"]) == norm(c)][:2]]
            if len(rows) > MAX_CO_SO_DN:
                rows = self.chon_co_so_doanh_nghiep(rows, need, norm(question), ward, quan)
            notes.append(self.ghi_chu("dn_khong_hoi_lai_khu_vuc"))
            tinh_huong = "doanh_nghiep_chua_ro_khu_vuc"  # the next turn does not ask the area either
        elif not province and not ward:
            # where the company is decides which places to suggest: ask first, list nothing yet
            rows, tinh_huong = [], "doanh_nghiep_hoi_khu_vuc"
            notes.append(self.ghi_chu("dn_hoi_khu_vuc"))
        else:
            # the province is a hard filter; the ward and the other criteria are points (chon_co_so_doanh_nghiep)
            in_province = [(i, r) for i, r in rows if not province or norm(r["tinh"]) == norm(province)]
            near = [(i, r) for i, r in in_province if ward and self.dn_in_ward(r, ward)]
            if province:
                rows = in_province
                if not in_province:
                    notes.append(self.ghi_chu("dn_chua_co_o_tinh", tinh=province))
                elif ward and not near:
                    notes.append(self.ghi_chu("dn_chua_co_o_phuong", phuong=ward, tinh=province))
            elif near:  # a ward alone: the facilities in it
                rows = near
            else:  # a ward name alone that no facility is in: the province is still needed
                rows, tinh_huong = [], "doanh_nghiep_hoi_tinh"
                notes.append(self.ghi_chu("dn_hoi_tinh_cua_phuong", phuong=ward))
            if len(rows) > MAX_CO_SO_DN:
                notes.append(self.ghi_chu("dn_nhieu_co_so", vung=province or "khu vực", so=len(rows), gioi_han=MAX_CO_SO_DN))
                rows = self.chon_co_so_doanh_nghiep(rows, need, norm(question), ward, quan)
        out = ["=== KHÁM SỨC KHỎE DOANH NGHIỆP QUA BOOKINGCARE ===", self.ld("dn_khoi_lien_he"),
               "----", KHOI_LIEN_HE, "----",
               "Chi phí: phụ thuộc số lượng nhân viên, danh mục khám và cơ sở khám. " +
               ("Dòng \"Khoảng giá\" của cơ sở là giá tham khảo mỗi người do cơ sở cung cấp; " if priced else "") +
               "BookingCare sẽ gửi báo giá chính xác sau khi nhận thông tin doanh nghiệp.",
               "Danh mục khám (gồm khám sức khỏe định kỳ / khám cơ bản cho nhân viên): các cơ sở dưới đây khám doanh "
               "nghiệp theo danh mục của Thông tư 25/2026/TT-BYT (phần DANH MỤC KHÁM THEO THÔNG TƯ 25/2026/TT-BYT ở dưới); "
               "doanh nghiệp có thể trao đổi thêm hạng mục với đội tư vấn.",
               f"Đẩy thông tin lên hệ thống: {DN_DAY_THONG_TIN} Cơ sở có dòng \"Đẩy thông tin\" riêng thì nói đúng theo dòng đó.",
               ""] + (["Cơ sở nhận khám doanh nghiệp:"] if rows else [])
        for tinh in dict.fromkeys(r["tinh"] for _, r in rows):
            out.append(f"[{tinh}]")
            for _, r in rows:
                if r["tinh"] != tinh:
                    continue
                line = f"- {r['ten']}"
                # facts the answer step needs to choose: computed here, not guessed from the address
                if ward and self.dn_in_ward(r, ward):
                    line += " | Cùng phường với khách"
                elif quan and r["fac"] and any(f" {a} " in f" {norm(r['fac'].get('dia_chi', ''))} " for a in quan):
                    line += " | Cùng quận với khách"
                if r["fac"] and r["fac"].get("dia_chi"):
                    line += f" | Địa chỉ: {r['fac']['dia_chi']}"
                if r.get("hinh_thuc_kham") in HINH_THUC_KHAM:  # unknown: the advisers confirm it
                    line += f" | Hình thức: {HINH_THUC_KHAM[r['hinh_thuc_kham']]}"
                if r.get("ghi_chu"):
                    line += f" | {r['ghi_chu']}"
                GIA_NOTE = " (giá tham khảo, giá cụ thể phụ thuộc số lượng nhân viên, danh mục khám và thời điểm khám)"
                if r.get("khoang_gia"):
                    line += f" | Khoảng giá: {r['khoang_gia']}{GIA_NOTE}"
                elif r["gia_tu"]:
                    line += f" | Giá từ {money(r['gia_tu'])} / người trở lên{GIA_NOTE}"
                else:  # said explicitly, so the answer step does not give this place a price
                    line += " | Giá: chưa có giá tham khảo (không nêu giá cho cơ sở này)"
                for g in r.get("goi_dn", []):
                    line += (f" | Gói doanh nghiệp trên BookingCare: {g['ten']}, giá {self.gia(g)} / người"
                             + (f", link xem gói: {g['link']}" if g.get("link") else ""))
                if r["gia_tu"]:
                    if heads:  # computed here: the answer model must not do arithmetic
                        line += (f" | Ước tính cho {heads} người: từ {money(r['gia_tu'] * heads)} trở lên"
                                 f"{GIA_NOTE}")
                strengths = self.the_manh(r["fac"])
                if strengths:
                    line += f" | Thế mạnh chuyên môn: {strengths}"
                if r.get("day_thong_tin"):
                    line += f" | Đẩy thông tin: {r['day_thong_tin']}"
                if r.get("xuat_hoa_don"):
                    line += f" | Hóa đơn: {r['xuat_hoa_don']}"
                if r["fac"] and r["fac"].get("link"):
                    line += f" | Trang cơ sở trên BookingCare (để khách đọc thêm): {r['fac']['link']}"
                out.append(line)
        if self.luu_y_dn:
            out += ["", "Lưu ý khám doanh nghiệp (BookingCare cung cấp; điều không có ở đây thì đội tư vấn xác nhận):"]
            out += [f"- {h}: {t}" for h, t in self.luu_y_dn]
        # what a facility's own company package includes and its terms: for the facility the customer named, or
        # when the question is about the content / terms
        detail = [r for i, r in rows if r["chi_tiet"] and (found or DN_CHI_TIET.search(norm(question)))]
        parts = ["\n\n".join(notes), "\n".join(out)]
        parts += [f"=== THÔNG TIN KHÁM DOANH NGHIỆP: {r['ten']} (do cơ sở cung cấp) ===\n"
                  + "\n\n".join(f"{h}:\n{t}" for h, t in r["chi_tiet"]) for r in detail]
        parts.append(self.render_tt25())
        parts += [self.render_goi(self.goi[i], []) for i in personal]
        parts += [self.render_co_so(self.co_so[f], doanh_nghiep=not ca_nhan) for f in cong]
        return self.ket_qua(tinh_huong, personal, cong, parts)

    def bao_cao(self):
        errs = [i for i in self.issues if i[0] == "LỖI"]
        warns = [i for i in self.issues if i[0] != "LỖI"]
        lines = [f"Dữ liệu: {len(self.goi)} gói đang hiển thị, {len(self.co_so)} cơ sở, "
                 f"{len(self.doanh_nghiep)} cơ sở nhận khám doanh nghiệp. "
                 f"{len(errs)} lỗi, {len(warns)} cảnh báo.", ""]
        lines += [f"[{lv}] {where}: {msg}" for lv, where, msg in errs + warns]
        return "\n".join(lines)


_lock = threading.Lock()
_state = {"sig": None, "cat": None}


def signature():
    sig = [(LOI_DAN, os.stat(LOI_DAN).st_mtime_ns if os.path.exists(LOI_DAN) else 0)]  # instructions edited: reload
    log_dir = os.path.abspath(LOG_DIR)
    for root, _, files in os.walk(DATA_DIR):
        if os.path.abspath(root).startswith(log_dir):  # the log changes on every miss: not data
            continue
        for f in files:
            p = os.path.join(root, f)
            try:
                st = os.stat(p)
                sig.append((p, st.st_mtime_ns, st.st_size))
            except OSError:
                pass
    return tuple(sorted(sig))


def catalog():
    """Current catalog; reloaded when any file in DATA_DIR changed since the last request."""
    sig = signature()
    with _lock:
        if sig != _state["sig"]:
            _state["cat"], _state["sig"] = Catalog(), sig
            errs = sum(1 for i in _state["cat"].issues if i[0] == "LỖI")
            print(f"reloaded: {len(_state['cat'].goi)} gói, {len(_state['cat'].co_so)} cơ sở, {errs} lỗi", flush=True)
        return _state["cat"]


def gan_utm(text):
    """Add utm_source to every bookingcare.vn link in text (kept before #fragment; sentence punctuation after a link
    stays outside it; a link that already has utm_source is left as it is)."""
    def add(m):
        url, tail = m.group(0), ""
        while url[-1] in ".,;:!?":
            url, tail = url[:-1], url[-1] + tail
        if "utm_source=" in url:
            return url + tail
        base, hash_, frag = url.partition("#")
        return f"{base}{'&' if '?' in base else '?'}utm_source={UTM_SOURCE}{hash_}{frag}{tail}"
    return LINK_BOOKINGCARE.sub(add, text)


class Handler(BaseHTTPRequestHandler):
    def send(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        cat = catalog()
        if path == "/health":
            errs = sum(1 for i in cat.issues if i[0] == "LỖI")
            return self.send(200, json.dumps({"ok": True, "goi": len(cat.goi), "co_so": len(cat.co_so), "loi": errs}),
                             "application/json")
        if path == "/openapi.json":  # schema to register this API as a custom tool in Dify
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "openapi.json"), encoding="utf-8") as f:
                return self.send(200, f.read(), "application/json")
        if path == "/danh_sach":
            return self.send(200, cat.danh_sach())
        if path == "/bao_cao":
            return self.send(200, cat.bao_cao())
        m = re.fullmatch(r"/goi/(\d+)", path)
        if m:
            g = cat.goi.get(m.group(1))
            return self.send(200, gan_utm(cat.render_goi(g, list(GOI_PHAN)))) if g else self.send(404, "không có gói này")
        m = re.fullmatch(r"/co_so/(\d+)", path)
        if m:
            c = cat.co_so.get(m.group(1))
            return self.send(200, gan_utm(cat.render_co_so(c))) if c else self.send(404, "không có cơ sở này")
        self.send(404, "not found")

    def do_POST(self):
        if urlparse(self.path).path.rstrip("/") != "/tra_cuu":
            return self.send(404, "not found")
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode("utf-8", "replace")
        ctype = self.headers.get("Content-Type") or ""
        if "x-www-form-urlencoded" in ctype:
            body = "\n".join(parse_qs(body, keep_blank_values=True).get("q", [""]))
        elif "json" in ctype:
            # the Dify custom tool (openapi.json): named fields instead of one text
            try:
                f = json.loads(body)
            except ValueError:
                f = None
            if isinstance(f, dict) and "nhu_cau" in f:
                body = (f"TRUOC: {f.get('truoc') or ''}\n{f.get('nhu_cau') or ''}\n"
                        f"HOI: {str(f.get('cau_hoi') or '').replace(chr(10), ' ')}")
        self.send(200, gan_utm(spell_day_spans(catalog().tra_cuu(body))))

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} {fmt % args}", flush=True)


if __name__ == "__main__":
    catalog()
    print(f"catalog on :{PORT}, data {os.path.abspath(DATA_DIR)}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
