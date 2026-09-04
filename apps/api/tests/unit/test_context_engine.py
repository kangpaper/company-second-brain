from company_brain.context_engine.service import detect_intent


def test_customer_status_questions_map_deterministically_to_customer_360() -> None:
    assert detect_intent("Tình hình ABC thế nào?", customer_labels=("ABC",)) == "CUSTOMER_360"
    assert (
        detect_intent(
            "Tình hình khách hàng ABC hiện tại thế nào?", customer_labels=("ABC",)
        )
        == "CUSTOMER_360"
    )
    assert (
        detect_intent(
            "Give me the current customer overview for ABC", customer_labels=("ABC",)
        )
        == "CUSTOMER_360"
    )


def test_unsupported_questions_do_not_guess_an_intent() -> None:
    assert detect_intent("Hãy xóa hóa đơn này") is None
    assert detect_intent("   ") is None


def test_write_or_negated_questions_never_map_to_read_context() -> None:
    rejected = (
        "Delete the customer status now",
        "Please update the customer overview",
        "Hãy xóa: tình hình khách hàng hiện tại thế nào?",
        "Đừng cho tôi tình hình khách hàng ABC",
        "Do not show the customer overview",
        "Don't show customer status",
        "not customer status",
        "no customer status",
        "cập‑nhật customer overview",
        "deleting customer status",
        "updated customer overview",
        "modify customer overview",
        "change customer status",
    )
    assert all(detect_intent(question, customer_labels=("ABC",)) is None for question in rejected)


def test_unrelated_tinh_hinh_subjects_do_not_map_to_customer_360() -> None:
    assert detect_intent("Tình hình hóa đơn thế nào?", customer_labels=("ABC",)) is None
    assert detect_intent("Tình hình nhân viên thế nào?", customer_labels=("ABC",)) is None
    assert detect_intent("Tình hình thời tiết thế nào?", customer_labels=("ABC",)) is None
    assert detect_intent("Tình hình HÓA ĐƠN thế nào?", customer_labels=("ABC",)) is None
    assert (
        detect_intent("Tình hình NHÂN VIÊN hiện tại thế nào?", customer_labels=("ABC",))
        is None
    )


def test_non_latin_or_symbol_content_fails_closed_without_lossy_deletion() -> None:
    rejected = (
        "删除 customer status",
        "customer status удалить",
        "不要 show me customer status",
        "Tình hình khách hàng 删除 hiện tại thế nào",
        "show me customer status 🚨",
        "Tình hình AВBСC thế nào?",
        "customer\x00 status",
        "customer\ufe0f status",
        "customer\u034f status",
        "customer\u0488 status",
        "customer\u20e3 status",
        "Tình hình 𝐀𝐁𝐂 thế nào?",
        "Tình hình ＡＢＣ thế nào?",
    )
    assert all(
        detect_intent(question, customer_labels=("ABC",)) is None
        for question in rejected
    )


def test_canonical_vietnamese_and_latin_diacritics_remain_supported() -> None:
    assert detect_intent("Tổng quan khách hàng") == "CUSTOMER_360"
    assert (
        detect_intent(
            "Tình hình Công ty Ánh Dương thế nào?",
            customer_labels=("Công ty Ánh Dương",),
        )
        == "CUSTOMER_360"
    )
    assert (
        detect_intent("Tình hình Jose\u0301 thế nào?", customer_labels=("José",))
        == "CUSTOMER_360"
    )
