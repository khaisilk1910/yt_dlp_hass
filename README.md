# YouTube-DLP cho Home Assistant

Tích hợp **YouTube-DLP** cho phép tìm kiếm và tải video hoặc âm thanh từ YouTube trực tiếp trong Home Assistant thông qua các Action.

## Tính năng

- Tải **video** hoặc **âm thanh** trực tiếp từ Home Assistant.
- Hỗ trợ lựa chọn chất lượng video và định dạng tệp đầu ra.
- Hỗ trợ các định dạng âm thanh phổ biến như MP3, M4A, Opus, FLAC và WAV.
- Có Action riêng cho tải video, tải âm thanh và Action kết hợp.
- Hỗ trợ tìm kiếm YouTube và trả về các thông tin như tiêu đề, ảnh thu nhỏ, thời lượng, kênh và liên kết video.
- Tác vụ tải chạy nền, không làm chặn luồng xử lý chính của Home Assistant.
- Hỗ trợ nhiều tác vụ tải liên tiếp và giới hạn số tác vụ chạy đồng thời để tránh gây quá tải hệ thống.
- Theo dõi tiến trình tải, tốc độ, dung lượng đã tải, tổng dung lượng và thời gian còn lại.
- Có cơ chế dự phòng khi YouTube trả về lỗi **HTTP 403**, đặc biệt khi tải âm thanh.
- Sử dụng FFmpeg của Home Assistant để xử lý và chuyển đổi media khi cần.
- Chỉ kiểm tra thư mục lưu, JavaScript runtime và các thành phần cần thiết khi thực sự có Action sử dụng, giúp quá trình khởi động Home Assistant nhẹ và nhanh hơn.
- Có thể bật/tắt thông báo khi tải hoàn tất cho từng nền tảng:
  - **Home Assistant Persistent Notification**.
  - **Thiết bị di động** sử dụng Home Assistant Companion App.
  - **Zalo Bot** với Thread ID, tài khoản Zalo và lựa chọn User/Group.
- Thông báo hoàn tất có thể hiển thị tên tệp, loại media, định dạng, chất lượng, dung lượng, đường dẫn lưu, URL nguồn và Job ID tương ứng với từng tác vụ tải.
- Việc gửi thông báo được thực hiện độc lập với tác vụ tải; lỗi gửi thông báo không làm thay đổi kết quả tải thành công.
- Toàn bộ cấu hình chính được thực hiện qua giao diện Home Assistant.
- Có thẻ **YouTube-DLP Media Center** tự đăng ký trong Home Assistant để phát YouTube, điều khiển loa, duyệt thư viện và tải media.
- Thẻ hỗ trợ lưu **nhạc yêu thích** bền vững trong Home Assistant, có ảnh thu nhỏ, thông tin bài, nút phát và phân trang.
- Có thể chọn một hoặc nhiều loa mặc định để phát cùng lúc, chỉnh tiêu đề thẻ và chọn 10 mẫu màu ngay trong trình chỉnh sửa UI của thẻ; giao diện tự thích nghi với theme sáng/tối.

## Cài đặt qua HACS

Nhấn nút dưới đây để mở trực tiếp repository này trong HACS:

[![Mở repository trong HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=khaisilk1910&repository=yt_dlp_hass&category=integration)

Sau đó:

1. Chọn **Download** trong HACS để cài đặt tích hợp.
2. Khởi động lại Home Assistant.
3. Vào **Settings → Devices & services → Add integration**.
4. Tìm **YouTube-DLP** và hoàn tất cấu hình theo giao diện.

## Cài đặt thủ công

1. Tải mã nguồn hoặc bản phát hành mới nhất của repository.
2. Sao chép thư mục:

```text
custom_components/yt_dlp
```

vào thư mục cấu hình Home Assistant:

```text
/config/custom_components/yt_dlp
```

Cấu trúc sau khi sao chép phải tương tự:

```text
/config/custom_components/yt_dlp/__init__.py
/config/custom_components/yt_dlp/manifest.json
/config/custom_components/yt_dlp/config_flow.py
...
```

3. Khởi động lại Home Assistant.
4. Vào **Settings → Devices & services → Add integration**.
5. Tìm **YouTube-DLP** và hoàn tất cấu hình.

## Bản quyền, điều khoản sử dụng và miễn trừ trách nhiệm

Tích hợp này được tạo nhằm phục vụ các nhu cầu hợp pháp và sử dụng cá nhân. Người dùng chỉ nên tải nội dung mà mình sở hữu, được chủ sở hữu cho phép tải xuống, thuộc phạm vi được pháp luật cho phép hoặc được phép sử dụng theo điều khoản của dịch vụ cung cấp nội dung.

Việc sử dụng tích hợp phải tuân thủ **Điều khoản dịch vụ của YouTube**, quy định về bản quyền, quyền sở hữu trí tuệ và pháp luật áp dụng tại nơi người dùng sinh sống. Không nên sử dụng tích hợp để sao chép, phân phối, lưu trữ hoặc khai thác trái phép nội dung có bản quyền.

Dự án này là một tích hợp độc lập, không phải sản phẩm chính thức và không được YouTube hoặc Google tài trợ, chứng nhận hay liên kết. YouTube có thể thay đổi hệ thống, API, cơ chế phân phối media hoặc biện pháp bảo vệ bất cứ lúc nào, vì vậy khả năng tải xuống có thể thay đổi hoặc ngừng hoạt động mà không báo trước.

Người dùng tự chịu trách nhiệm về cách sử dụng tích hợp và nội dung mình tải xuống. Tác giả/người duy trì dự án không chịu trách nhiệm đối với việc sử dụng sai mục đích, vi phạm bản quyền hoặc điều khoản dịch vụ, hạn chế tài khoản/mạng, mất dữ liệu, gián đoạn dịch vụ hay bất kỳ thiệt hại trực tiếp hoặc gián tiếp nào phát sinh từ việc cài đặt hoặc sử dụng tích hợp này.
