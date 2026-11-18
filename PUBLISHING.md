# Công bố repository và lấy DOI (GitHub + Zenodo)

1. Tạo repository công khai trên GitHub, ví dụ `bizkb-a`, dưới tài khoản hoặc tổ chức đã chọn.
2. Đăng nhập https://zenodo.org bằng tài khoản GitHub, vào **Settings → GitHub**, bật công tắc cho repository vừa tạo.
3. Cập nhật các chỗ `TODO` trong `CITATION.cff` và `.zenodo.json` (tác giả, ORCID, đơn vị).
4. Đẩy mã lên:
   ```bash
   git remote add origin https://github.com/ORG_OR_USER/bizkb-a.git
   git push -u origin main
   ```
5. Trên GitHub, tạo **Release** với tag `v0.5.0`. Zenodo tự lưu bản chụp và cấp DOI (dạng `10.5281/zenodo.NNNNNNN`).
6. Thay link và DOI vào: mục *Data availability* của `paper.tex`, `CITATION.cff`, `README.md`.

Lưu ý: DOI gắn với một release cụ thể, nên bài báo trỏ đúng phiên bản mã đã dùng để ra số liệu.
