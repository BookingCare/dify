# Hướng dẫn sửa dữ liệu chatbot gói khám

Thư mục này là **nguồn dữ liệu duy nhất** của chatbot. Sửa file xong là chatbot dùng dữ liệu mới ngay ở câu hỏi tiếp theo, không cần khởi động lại gì.

Kiểm tra dữ liệu sau khi sửa: mở **http://localhost:8100/bao_cao**
- `[LỖI]`: dòng đó bị bỏ qua, chatbot sẽ không thấy. Phải sửa.
- `[CẢNH BÁO]`: thiếu thông tin hoặc nội dung chưa được duyệt; chatbot vẫn chạy, phần thiếu sẽ trả lời "chưa có thông tin".

Xem một gói / cơ sở đúng như chatbot đọc: http://localhost:8100/goi/3758, http://localhost:8100/co_so/4

## Cấu trúc

```
du_lieu/
  goi/                          GÓI KHÁM
    goi.csv                     mỗi dòng = 1 gói
    lich.csv                    mỗi dòng = 1 buổi khám của 1 gói
    danh_muc.csv                mỗi dòng = 1 hạng mục khám của 1 gói
    noi_dung/<ma_goi>.md        giới thiệu, chuẩn bị, lưu ý giấy tờ của gói
  co_so/                        CƠ SỞ Y TẾ (mỗi cơ sở một mã, chỉ khai ở co_so.csv)
    co_so.csv                   mọi cơ sở: tư (loai tu) và bệnh viện công (loai cong)
    noi_dung/<ma_co_so>.md      giới thiệu, đường đi, bảo hiểm... của cơ sở tư
    hop_tac_doanh_nghiep.csv    cơ sở nhận khám doanh nghiệp qua BookingCare (theo mã)
    kham_tong_quat.csv          thông tin khám tổng quát của bệnh viện công (theo mã)
  kien_thuc/                    NỘI DUNG CHUNG (file .md, mỗi mục "## " là một ý)
    giay_to/<loai>.md           lưu ý chung cho 6 loại giấy khám
    doanh_nghiep/luu_y.md       lưu ý khám doanh nghiệp từ Product / Business
    doanh_nghiep/co_so/<ma_co_so>.md   danh mục gói + lưu ý khám doanh nghiệp riêng của một cơ sở hợp tác
    doanh_nghiep/danh_muc_tt25.md   danh mục khám theo Mẫu số 03 Thông tư 25/2026
    hoi_dap/<chu_de>.md         hỏi đáp chung (vd chuan_bi_di_kham.md); thêm file mới = thêm chủ đề
```

Ngoài thư mục này: `../nhat_ky/thieu_du_lieu.csv` (chatbot tự ghi câu hỏi chưa có dữ liệu, không sửa), `../he_thong/` (phần kỹ thuật), `../luu_tru/tai_lieu/` (văn bản quy định).

File `.csv` mở bằng Excel hoặc Google Sheets. Khi lưu, chọn **CSV UTF-8**, giữ nguyên dòng tiêu đề và tên cột. File `.md` mở bằng bất kỳ trình soạn thảo nào.

**Mã là chìa khoá:** gói nối với cơ sở bằng `ma_co_so`; danh sách doanh nghiệp và thông tin khám tổng quát cũng chỉ ghi `ma_co_so`. Tên, địa chỉ, điện thoại, tỉnh của cơ sở chỉ sửa ở `co_so/co_so.csv`.

## goi/goi.csv

| Cột | Ý nghĩa | Ví dụ |
|---|---|---|
| ma_goi | Mã gói (số, không trùng). Cũng là đuôi `-i<ma>` của link | 3758 |
| ten | Tên gói, nên giữ ký hiệu trong ngoặc, khách hay gọi theo ký hiệu | Gói Khám "Bác Sĩ Khuyến Cáo" (DC2) |
| ma_co_so | Mã cơ sở trong co_so/co_so.csv | 450 |
| noi_kham | Chỉ điền khi gói không gắn cơ sở nào (chuỗi nhiều nơi) | Hệ thống phòng khám Dr.Marie |
| tinh | Chỉ điền khi không có ma_co_so (nếu có thì lấy theo cơ sở) | Hà Nội |
| gia_tu | Giá thấp nhất, đồng, chỉ chữ số | 3000000 |
| gia_den | Giá cao nhất nếu là khoảng giá, để trống nếu một giá | 3824000 |
| link | Link xem chi tiết gói | https://bookingcare.vn/... |
| gioi_tinh | `nam`, `nu` hoặc để trống (dùng chung) | nu |
| muc_dich | Một hoặc nhiều giá trị, cách nhau bằng `\|` | tong_quat |
| thoi_gian_tra_ket_qua | Thời gian có kết quả / trả giấy, để trống nếu chưa rõ | Trong ngày |
| hien_thi | `1` hiện, `0` tạm ẩn khỏi chatbot | 1 |

Giá trị `muc_dich`:
- Khám cá nhân: `tong_quat`, `ung_thu`, `chuyen_khoa`, `tien_hon_nhan`, `tre_em`.
- Doanh nghiệp: `doanh_nghiep`.
- 6 loại giấy: `lai_xe`, `di_lam_di_hoc`, `work_permit`, `visa`, `du_hoc`, `di_nhat`.

Gói `tien_hon_nhan`, `tre_em`, `doanh_nghiep` và gói 6 loại giấy chỉ được gợi ý khi khách hỏi đúng mục đích đó. Gói vừa tổng quát vừa tầm soát ung thư ghi `tong_quat|ung_thu`.

## goi/lich.csv

| Cột | Giá trị |
|---|---|
| ma_goi | mã gói |
| thu | `T2` `T3` `T4` `T5` `T6` `T7` `CN` |
| buoi | `Sáng`, `Chiều` |
| khung_gio | các giờ, cách nhau bằng dấu phẩy: `06:00, 07:30` |

## goi/danh_muc.csv

| Cột | Ý nghĩa |
|---|---|
| ma_goi | mã gói |
| nhom | nhóm hạng mục: `Khám lâm sàng`, `Xét nghiệm`, `Chẩn đoán hình ảnh và thăm dò chức năng`... |
| thu_tu | thứ tự trong nhóm (1, 2, 3...) |
| hang_muc | tên hạng mục, chatbot dùng cột này để tìm gói theo hạng mục (vd "nội soi dạ dày") |
| mo_ta | mô tả ngắn, có thể để trống |

## goi/noi_dung/<ma_goi>.md

```
# Tên gói

## Giới thiệu
...

## Chuẩn bị trước khi khám
- ...
```

Giữ đúng hai tiêu đề `## Giới thiệu` và `## Chuẩn bị trước khi khám`. Thiếu mục nào thì chatbot báo "chưa có thông tin" cho mục đó. Gói giấy khám có thể thêm `## Lưu ý giấy tờ` cho điều riêng của gói; khi khác với `kien_thuc/giay_to/`, chatbot theo lưu ý của gói.

## co_so/co_so.csv

| Cột | Ý nghĩa |
|---|---|
| ma_co_so | mã cơ sở trên BookingCare (số, không trùng). Bệnh viện công chưa có mã BookingCare dùng mã tạm `9xxxx`, đổi sang mã thật khi có (sửa cả ở kham_tong_quat.csv) |
| ten | tên cơ sở |
| loai | `tu` (đặt được gói qua BookingCare) hoặc `cong` (bệnh viện công, chatbot chỉ hướng dẫn) |
| ten_goi_khac | các tên khách hay gọi, cách nhau bằng `\|` (viết tắt, không dấu: `bv 108\|bach mai`). Chatbot dùng cột này để nhận ra cơ sở |
| dia_chi | địa chỉ theo mô hình 2 cấp |
| phuong_xa | tên phường/xã mới, đúng như danh mục `phường xã mới.csv` |
| tinh | `Hà Nội`, `Thành phố Hồ Chí Minh`, `Nghệ An`... |
| dien_thoai | số điện thoại |
| bao_hiem | `co`, `khong`, hoặc để trống nếu chưa rõ |
| thuoc_he_thong | mã cơ sở cha (vd 508 = Hệ thống Bệnh viện Hồng Ngọc), để trống nếu không |
| link | trang cơ sở trên BookingCare |
| website | website riêng của cơ sở (chatbot đưa cho bệnh viện công) |
| hien_thi | `1` hiện, `0` tạm ẩn (bệnh viện công chưa có dữ liệu thì để `0`) |

Chatbot cũng hiểu tên kèm chi nhánh ("BV Đại học Y cơ sở Hoàng Mai") và tên viết tắt theo chữ cái đầu ("BVĐHY").

## co_so/noi_dung/<ma_co_so>.md

Mỗi mục là một tiêu đề `## ...`, đặt tên tự do, ví dụ `## Giới thiệu chung`, `## Hướng dẫn đường đi và thủ tục khám`, `## Chính sách Bảo hiểm y tế`. Chatbot đọc các mục này khi khách hỏi về cơ sở tư. Các mục về giá khám lẻ và đặt lịch khám lẻ (`## Chi phí...`, `## Giá...`, `## Cách thức đặt lịch`) không được dùng, vì chatbot chỉ tư vấn gói.

## co_so/hop_tac_doanh_nghiep.csv

Cơ sở nhận khám doanh nghiệp qua BookingCare. Khách hỏi khám cho công ty, nhân viên thì chatbot chỉ giới thiệu các cơ sở trong file này (ưu tiên đúng tỉnh, tối đa 3 cơ sở), chỉ nêu giá khi có cột `khoang_gia` (chép nguyên văn, nói là giá tham khảo) hoặc `gia_tu`; khách nêu số người thì hệ thống tự tính mức ước tính từ `gia_tu`, không nêu số điện thoại cơ sở, luôn kèm link đăng ký tư vấn doanh nghiệp. Cơ sở không có trong file thì chatbot nói BookingCare chưa hỗ trợ khám doanh nghiệp ở đó.

| Cột | Ý nghĩa | Ví dụ |
|---|---|---|
| ma_co_so | Mã trong co_so/co_so.csv (bắt buộc). Tên, địa chỉ, tỉnh lấy từ đó | 508 |
| ten_hien_thi | Tên riêng khi giới thiệu cho doanh nghiệp, để trống thì dùng tên trong co_so.csv | Phòng khám DYM Medical Center (Hà Nội) |
| hinh_thuc_kham | `tai_co_so`, `tai_doanh_nghiep`, `ca_hai`; để trống nếu chưa rõ (chatbot nói đội tư vấn xác nhận) | ca_hai |
| ghi_chu | Câu chatbot được nói thêm (không ghi giá, số điện thoại) | Nhận đoàn từ 20 người |
| gia_tu | Giá thấp nhất mỗi người, đồng, chỉ chữ số. Chỉ điền khi đó thật là mức sàn ("tối thiểu", "từ"); dùng để tính ước tính theo số người. Giá "khoảng" thì để trống | 2000000 |
| khoang_gia | Giá doanh nghiệp đúng như cơ sở cung cấp; chatbot chép nguyên văn, nói là giá tham khảo | Khoảng 800.000đ/người |
| day_thong_tin | Cơ sở có đẩy kết quả lên Cơ sở dữ liệu sức khỏe của Bộ Y tế không (để trống: theo câu chung của BookingCare) | Chỉ với khách khám đoàn |
| xuat_hoa_don | Cách xuất hóa đơn | Có xuất hóa đơn về công ty |
| ngay_cap_nhat | Ngày kiểm tra dòng này gần nhất | 2026-11-02 |
| hien_thi | `1` hiện, `0` tạm ẩn | 1 |

Danh mục gói và lưu ý khác (ưu đãi, lấy máu tại công ty, thời gian khám...) của từng cơ sở: file `kien_thuc/doanh_nghiep/co_so/<ma_co_so>.md`, hai mục `## Danh mục gói khám doanh nghiệp` và `## Lưu ý khác`. Chatbot chỉ đưa khi khách nêu tên cơ sở hoặc hỏi về danh mục, hóa đơn, lấy máu, ưu đãi...

Link đăng ký, hotline và Zalo BookingCare cho doanh nghiệp đặt cố định trong `he_thong/catalog/app.py`: https://bookingcare.vn/suc-khoe-doanh-nghiep/dang-ky (mọi link bookingcare.vn được tự gắn utm_source=AI_goikham), 024-7301-2468, https://zalo.me/3344993609824066411.

## co_so/kham_tong_quat.csv

Thông tin khám tổng quát của bệnh viện công (cơ sở có `loai` = `cong` trong co_so.csv), mỗi bệnh viện một dòng theo `ma_co_so`. Chatbot hướng dẫn khách tự đi khám; không đưa link đặt lịch BookingCare.

| Cột | Ý nghĩa | Bắt buộc |
|---|---|---|
| ma_co_so | Mã bệnh viện trong co_so.csv | Có |
| hinh_thuc_kham | Có gói của bệnh viện hay đăng ký khám Nội rồi bác sĩ chỉ định | Có |
| noi_dang_ky | Khu / tòa / cơ sở tiếp nhận | Có |
| cach_dat_lich | Website, app, tổng đài hay lấy số trực tiếp | Có |
| thoi_gian_kham | Ngày, giờ; thứ 7, chủ nhật có khám không | Có |
| chi_phi, link_chi_phi | Chi phí tham khảo và link (ghi rõ nguồn giá nếu không phải bệnh viện công bố) | Nếu có |
| bhyt | BHYT có áp dụng cho khám tổng quát không | Có |
| luu_y | Giấy tờ, nhịn ăn, giờ nên đến | Nên có |
| nguon, ngay_cap_nhat | Lấy thông tin từ đâu; ngày kiểm tra gần nhất | Nên có |

**Nhập cả danh sách bệnh viện công** (file CSV có các cột name, address, phone, website, working_hours, description, medical_workflow, pricing...): `python3 he_thong/scripts/nhap_bv_cong.py "<file.csv>"` (thêm `--thu` để xem trước). Bệnh viện đã có giữ nguyên dữ liệu viết tay, chỉ điền chỗ trống; bệnh viện mới mã `91xxx`; khoa / trung tâm / cơ sở 2 của một bệnh viện mã `92xxx`, `thuoc_he_thong` là bệnh viện đó và được liệt kê trong khối của bệnh viện. Phần còn lại vào `co_so/noi_dung/<ma>.md` (dòng đầu `<!-- nhap_bv_cong -->`: chạy lại sẽ ghi đè; bỏ dòng này nếu sửa tay). Chạy lại nhiều lần không sinh bản trùng.

**Thêm một bệnh viện công:** điền dòng của bệnh viện trong co_so.csv (địa chỉ, điện thoại, website), điền dòng trong kham_tong_quat.csv, rồi đổi `hien_thi` trong co_so.csv thành `1`. Khách hỏi khám cho nhân viên tại bệnh viện công thì chatbot hướng dẫn liên hệ bệnh viện và giới thiệu thêm các cơ sở hợp tác doanh nghiệp.

## kien_thuc/

Mọi file đều có phần đầu:

```
---
cap_nhat: 2026-11-02
duyet: chua        ← đổi thành "co" khi PO / đội tư vấn đã duyệt câu chữ
nguon: ...         ← (không bắt buộc) link văn bản gốc
---
# Tiêu đề

## Mục 1
Nội dung...
```

Mỗi mục `## ` là một ý; mục để trống thì chatbot nói "chưa có thông tin". File có nội dung mà chưa `duyet: co` sẽ hiện `[CẢNH BÁO]` ở trang báo cáo.

- **giay_to/<loai>.md**: `lai_xe`, `di_lam_di_hoc`, `work_permit`, `visa`, `du_hoc`, `di_nhat`. Lưu ý chung cho mọi gói cùng loại giấy. Ghi nguồn ở cuối mỗi mục (`Nguồn: ... – link`).
- **doanh_nghiep/luu_y.md**: quy trình đăng ký, số nhân viên tối thiểu, khám tại doanh nghiệp hay tại cơ sở, thời gian trả kết quả, hợp đồng và hóa đơn, lưu ý khác.
- **doanh_nghiep/danh_muc_tt25.md**: danh mục khám theo Mẫu số 03 Thông tư 25/2026. Chatbot dùng khi khách doanh nghiệp hỏi khám những gì, hoặc khách hỏi về Thông tư 25 / Mẫu số 03. Phần đầu có `tieu_de` và `nguon` (link mẫu giấy khám).
- **hoi_dap/<chu_de>.md**: hỏi đáp chung, mỗi mục `## ` là một câu hỏi. Hiện có `chuan_bi_di_kham.md` (nhịn ăn, uống thuốc, mang theo gì, kinh nguyệt...). Chatbot dùng khi khách hỏi chung, chưa chọn gói; khi đã chọn gói thì hướng dẫn của gói được ưu tiên.

## Thao tác thường gặp

- **Thêm gói:** thêm 1 dòng `goi/goi.csv`, các dòng lịch vào `goi/lich.csv`, các hạng mục vào `goi/danh_muc.csv`, tạo `goi/noi_dung/<ma>.md`.
- **Đổi giá / lịch:** sửa đúng dòng trong `goi/goi.csv` / `goi/lich.csv`.
- **Tạm ngừng gói / cơ sở:** đặt `hien_thi` = `0` (giữ được dữ liệu để bật lại).
- **Xoá hẳn một gói:** xoá dòng ở `goi/goi.csv`, các dòng của gói ở `goi/lich.csv`, `goi/danh_muc.csv`, và file `.md`. Báo cáo sẽ cảnh báo nếu còn sót.
- **Thêm cơ sở tư:** thêm dòng `co_so/co_so.csv` và file `co_so/noi_dung/<ma>.md`, rồi điền `ma_co_so` cho các gói.
- **Khách gọi cơ sở bằng tên lạ:** thêm tên đó vào cột `ten_goi_khac` của cơ sở.
- **Thêm chủ đề hỏi đáp:** tạo file mới trong `kien_thuc/hoi_dap/`, mỗi câu hỏi một mục `## `.

## Lưu ý

- Mọi thay đổi được git ghi lại: sau mỗi đợt sửa, nhờ kỹ thuật commit (hoặc tự chạy `git add -A && git commit -m "mô tả"` ở thư mục dify-local) để xem lại lịch sử và quay lại bản cũ khi cần.
- `he_thong/scripts/chuyen_du_lieu.py` tạo lại `goi/` và các cơ sở tư trong `co_so/` từ bản xuất hệ thống; phần `kien_thuc/`, danh sách doanh nghiệp, bệnh viện công được giữ nguyên. Bản cũ được chép vào `luu_tru/du_lieu.bak-<thời gian>`.
- Chỉnh câu lệnh hoặc luồng chatbot ở `he_thong/scripts/build_chatflow.py` và `he_thong/bot/prompt_answer.txt`, không liên quan thư mục này.
