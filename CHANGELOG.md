# Changelog

## 0.5.34 — 2026-11-06

- Chuẩn hoá metadata của integration/Marketplace cho Home Assistant 2026.11 và cấu hình phiên bản tối thiểu trong `hacs.json`.
- Sử dụng bản dịch trong `translations/`, bỏ `strings.json` dư thừa gây thất bại trong workflow kiểm tra của repository.
- Giữ khởi động config entry không chờ I/O hoặc các module phụ; thêm thông tin log mức độ sẵn sàng từng thành phần sau khi HA khởi động.
- Đăng ký thẻ Lovelace an toàn khi tài nguyên được tải trễ, tránh tài nguyên trùng và cập nhật URL theo phiên bản; hỗ trợ cả chế độ lưu storage và YAML.
- Bổ sung `diagnostics.py` cung cấp trạng thái runtime, số tải xuống và trạng thái module nhưng không đưa vào URL/tokens/tên nội dung/đường dẫn/cấu hình thông báo.
- Chuyển kiểm tra tồn tại các file cache DLNA sang executor để hạn chế I/O trên event loop. Giữ nguyên cơ chế chuẩn bị file MP3 DLNA để không làm thay đổi tương thích của các loa cũ.
- Cải thiện thanh tiến trình của thẻ khi thời lượng xuất hiện trễ; giới hạn độ mờ nền và bỏ dữ liệu tiến trình không hợp lệ.
- Chuẩn hoá các workflow kiểm tra HACS, hassfest, Python và JavaScript; bổ sung kiểm thử hồi quy.
- Không thay đổi cấu trúc dữ liệu lưu Favorites, service schema hay đường dẫn API hiện hành.

### Ghi chú nâng cấp

- Yêu cầu Home Assistant 2026.11.0 trở lên cho phiên bản Marketplace này.
- Khi nâng cấp integration đã được tải, khởi động lại Home Assistant theo Repairs để áp dụng mã Python mới. Làm mới trình duyệt mạnh nếu trình duyệt vẫn cache thẻ JavaScript cũ.
- Đối với phát trực tuyến qua DLNA, phiên bản này **không** thay đổi luồng chuyển mã/cache MP3 hiện có. Việc đo thời lượng tải, hành vi seek và tính tương thích của từng thiết bị cần kiểm tra với loa thật.
