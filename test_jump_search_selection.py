import unittest
from unittest import mock

from jump_search import JumpSearchMixin


class JumpSearchSelectionTests(unittest.TestCase):
    def test_retry_query_is_focused_and_selected_for_replacement(self):
        dialog = mock.Mock()
        text_ctrl = dialog.GetTextCtrl.return_value
        text_ctrl.GetValue.return_value = "failed query"

        JumpSearchMixin._select_jump_dialog_text(dialog)

        text_ctrl.SetFocus.assert_called_once_with()
        text_ctrl.SetSelection.assert_called_with(0, len("failed query"))

    def test_missing_text_control_is_safe(self):
        dialog = mock.Mock()
        dialog.GetTextCtrl.side_effect = AttributeError

        JumpSearchMixin._select_jump_dialog_text(dialog)

    def test_finds_text_control_as_child_when_dialog_has_no_getter(self):
        non_text_child = object()

        class Dialog:
            def GetChildren(self):
                return [non_text_child, text_ctrl]

        text_ctrl = mock.Mock()
        text_ctrl.GetValue.return_value = "old search"

        JumpSearchMixin._select_jump_dialog_text(Dialog())

        text_ctrl.SetFocus.assert_called_once_with()
        text_ctrl.SetSelection.assert_called_once_with(0, len("old search"))


if __name__ == "__main__":
    unittest.main()
