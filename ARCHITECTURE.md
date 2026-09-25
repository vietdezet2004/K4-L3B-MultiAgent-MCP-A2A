# L3B Architecture Record

## 1. System overview

Kiến trúc phối hợp đa tác tử (A2A - Agent-to-Agent) theo chuẩn của cuộc thi:

```text
                          ┌──────────────────────────┐
                          │   Coordinator / Router   │
                          └─────────────┬────────────┘
                                        │ (Handoff)
         ┌──────────────────────────────┼──────────────────────────────┐
         ▼                              ▼                              ▼
┌──────────────────┐           ┌──────────────────┐           ┌──────────────────┐
│ Order/Item Agent │           │  Payment Agent   │           │  Shipment Agent  │
└────────┬─────────┘           └────────┬─────────┘           └────────┬─────────┘
         │                              │                              │
         └──────────────────────────────┼──────────────────────────────┘
                                        │ (MCP Evidence Collector)
                                        ▼
                               ┌──────────────────┐
                               │   Policy Agent   │
                               └────────┬─────────┘
                                        │ (Handoff)
                                        ▼
                               ┌──────────────────┐
                               │  Verifier Agent  │
                               └────────┬─────────┘
                                        │ (Validated Output)
                                        ▼
                                   [END OUTPUT]
```

- Mọi tương tác giữa các Agent đều phát sinh các observable trace events (`task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided`, `verification_completed`).
- Dữ liệu thu thập từ MCP Gateway được quản lý thông qua lớp `InCaseCache`, đảm bảo không gọi lặp tool với cùng tham số trong một case, tối ưu hóa điểm `efficiency`.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Coordinator / Router | `case` object từ input | Điều phối luồng xử lý, phân công nhiệm vụ, tổng hợp output cuối | Không gọi tool trực tiếp | Giao việc cho Order, Payment, Shipment Agents |
| Order/Item Agent | `candidate_order_ids`, `customer_unique_id_hint` | Xác định đơn hàng thực tế, loại trừ candidate giả, trích xuất item và seller | `get_order`, `get_customer_history`, `get_order_items`, `get_product_context` | `resolved_order_ids`, `rejected_candidates`, `item_ids`, `seller_ids` |
| Shipment Agent | `resolved_order_id` | Phân tích lộ trình vận chuyển, so sánh mốc giao hàng, phát hiện trễ do seller hay logistics | `get_shipment_summary` | `shipment_verdict`, `late_seller_ids`, `timeline_complete` |
| Payment Agent | `resolved_order_id`, claim topics | Đối soát thanh toán, phát hiện duplicate charge, kiểm tra trạng thái hoàn tiền | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment_verdict`, `captured_total_brl`, `refunded_total_brl` |
| Policy Agent | Claims, policy version, kết quả từ các specialists | Áp dụng chính sách bồi hoàn, giải quyết mâu thuẫn giữa lời khai khách hàng và chứng cứ | `get_policy` | `primary_issue`, `case_status`, `financial_resolution`, `data_conflicts` |
| Verifier Agent | Toàn bộ output đề xuất | Kiểm tra tính bất biến (invariants), đối soát giới hạn hoàn tiền, kiểm tra schema | Không gọi tool trực tiếp | Output đã xác thực, hoàn tất trace |

## 3. Entity resolution và A2A protocol

- **Chiến lược xếp hạng candidate**: Duyệt qua danh sách `candidate_order_ids`. Ứng viên nào gọi thành công `get_order` và khớp với hồ sơ khách hàng sẽ được đưa vào `resolved_order_ids` với `confidence = 1.0`. Các ứng viên thất bại hoặc không hợp lệ lập tức được đưa vào `rejected_candidates`.
- **A2A Protocol**:
  - Giao tiếp giữa các Agent theo cấu trúc message chuẩn, tương ứng với các sự kiện trong `TraceWriter`.
  - Định danh mọi sự kiện theo `case_id`.
  - Chuyển giao trách nhiệm qua sự kiện `handoff` có gắn `decision_code`.

## 4. Evidence và conflict lifecycle

- **Validation & Provenance**: Mọi phản hồi từ MCP Gateway được kiểm tra hợp lệ theo `mcp-evidence-response-v1.schema.json`. Mã `evidence_ref` được lưu trữ nguyên vẹn và liên kết trực tiếp vào output cũng như trace.
- **Scope Isolation**: Bằng chứng chỉ được sử dụng trong phạm vi case đang xét, không chia sẻ chéo giữa các case.
- **Conflict Handling**: Khi có sự khác biệt giữa yêu cầu khách hàng (ví dụ: `requested_full_refund`) và chính sách bồi hoàn (`system_policy`), hệ thống ghi nhận đối tượng vào `data_conflicts`, ưu tiên áp dụng nguồn `system_policy` với resolution code rõ ràng.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | 2 lần | Bỏ qua endpoint phụ, giữ giá trị an toàn | `tool_result_consumed` / null |
| Entity not found/ambiguous | 0 (deterministic) | Đánh dấu `status="not_found"`, gán `confidence=0.0` | `handoff` / `entities_resolved` |
| Source conflict | Không retry | Chọn `system_policy` theo quy tắc ưu tiên | `policy_decided` / `APPLY_POLICY_*` |
| Invalid specialist result | 1 lần | Trả về verdict mặc định (`on_time` / `reconciled`) | `verification_completed` |

- **Efficiency Policy**: Sử dụng bộ đệm in-case cache lưu trữ kết quả tool. Giới hạn số lượt gọi MCP ở mức 6 - 9 calls/case, đảm bảo nằm sâu trong vùng an toàn của per-case call budget.

## 6. Verification invariants

Trước khi xuất file output, Verifier kiểm tra các bất biến kỹ thuật:
1. `recommended_refund_brl <= refundable_total_brl`.
2. Toàn bộ các danh sách định danh (`item_ids`, `seller_ids`, `payment_references`, `related_order_ids`) đều có phần tử duy nhất (`uniqueItems: true`).
3. Mọi `evidence_ref` trong output phải nằm trong danh sách evidence đã được ghi nhận qua MCP Gateway.
4. Trách nhiệm của bên gây lỗi (`responsible_parties`) và người bán trễ hạn (`late_seller_ids`) phải đồng nhất với kết luận `primary_issue`.
5. Sự kiện `verification_completed` được phát trước khi kết thúc xử lý.

## 7. Reproducibility

- **Môi trường**: Python >= 3.11.
- **Các thư viện phụ thuộc**: Đã được cố định trong `pyproject.toml` (`mcp`, `httpx2`, `jsonschema`, `referencing`).
- **Lệnh thực thi**: `day09 run`
- **Lệnh kiểm tra tính hợp lệ**: `day09 validate`
- **Lệnh đóng gói**: `day09 package --output dist/submission.zip`
