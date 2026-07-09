"""python-kasa 0.10+ exception aliases and runtime patches for the Kasa plugin."""

from typing import Any

from kasa.exceptions import AuthenticationError, DeviceError, KasaException

# Renamed in python-kasa 0.10: SmartDeviceException -> DeviceError
SmartDeviceException = DeviceError

_PATCHED = False


def apply_kasa_patches():
    """Apply small python-kasa fixes needed for Tapo hub/camera child lists."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True
    try:
        from kasa.discover import Discover
        from kasa.exceptions import SmartErrorCode
        from kasa.protocols.smartprotocol import SmartProtocol
        from kasa.smart.smartdevice import SmartDevice
        from kasa.smartcam.smartcamdevice import SmartCamDevice
    except ImportError:
        return

    _get_conn_params_orig = Discover._get_connection_parameters

    @staticmethod
    def _get_connection_parameters(discovery_result):
        """Handle SMART.IPCAMERA discovery with encrypt_type 3 but no sym_schm."""
        encrypt_schm = discovery_result.mgt_encrypt_schm
        encrypt_type = None
        if encrypt_schm is not None:
            encrypt_type = encrypt_schm.encrypt_type
            if not encrypt_type and discovery_result.encrypt_info is not None:
                encrypt_type = discovery_result.encrypt_info.sym_schm
        if (
            not encrypt_type
            and discovery_result.device_type == 'SMART.IPCAMERA'
            and encrypt_schm is not None
            and encrypt_schm.is_support_https
            and discovery_result.encrypt_type
        ):
            encrypt_type = 'AES'
        if encrypt_type and encrypt_schm is not None:
            if not encrypt_schm.encrypt_type:
                encrypt_schm.encrypt_type = encrypt_type
            if not encrypt_schm.lv and discovery_result.encrypt_type:
                encrypt_schm.lv = max(int(i) for i in discovery_result.encrypt_type)
        return _get_conn_params_orig(discovery_result)

    Discover._get_connection_parameters = _get_connection_parameters

    async def _handle_response_lists(
        self,
        response_result: dict[str, Any],
        method: str,
        params: dict | None,
        retry_count: int,
    ) -> None:
        if (
            response_result is None
            or isinstance(response_result, SmartErrorCode)
            or "start_index" not in response_result
            or (list_sum := response_result.get("sum")) is None
        ):
            return

        list_keys = [
            key
            for key in response_result
            if isinstance(response_result[key], list)
        ]
        if not list_keys:
            return
        response_list_name = list_keys[0]
        while (list_length := len(response_result[response_list_name])) < list_sum:
            request = self._get_list_request(method, params, list_length)
            response = await self._execute_query(
                request,
                retry_count=retry_count,
                iterate_list_pages=False,
            )
            next_batch = response[method]
            if not next_batch[response_list_name]:
                break
            response_result[response_list_name].extend(next_batch[response_list_name])

    SmartProtocol._handle_response_lists = _handle_response_lists
    try:
        from kasa.protocols.smartcamprotocol import SmartCamProtocol
    except ImportError:
        SmartCamProtocol = None
    if SmartCamProtocol is not None:
        SmartCamProtocol._handle_response_lists = _handle_response_lists

    _create_delete_children_orig = SmartDevice._create_delete_children

    async def _create_delete_children(
        self,
        child_device_resp: dict[str, list],
        child_device_components_resp: dict[str, list],
    ) -> bool:
        if not isinstance(child_device_components_resp, dict):
            child_device_components_resp = {}
        components = child_device_components_resp.get("child_component_list")
        if components is None:
            child_device_components_resp = dict(child_device_components_resp)
            child_device_components_resp["child_component_list"] = []
        if not isinstance(child_device_resp, dict):
            child_device_resp = {}
        if child_device_resp.get("child_device_list") is None:
            child_device_resp = dict(child_device_resp)
            child_device_resp["child_device_list"] = []
        return await _create_delete_children_orig(
            self,
            child_device_resp,
            child_device_components_resp,
        )

    SmartDevice._create_delete_children = _create_delete_children

    async def _smartcam_update_children_info(self) -> bool:
        changed = False
        child_info = self._try_get_response(self._last_update, "getChildDeviceList", {})
        if not child_info:
            return changed
        components = self._try_get_response(
            self._last_update,
            "getChildDeviceComponentList",
            {},
        ) or {}
        changed = await self._create_delete_children(child_info, components)
        for info in child_info.get("child_device_list", []):
            child_id = info.get("device_id")
            if child_id not in self._children:
                continue
            self._children[child_id]._update_internal_state(info)
        return changed

    SmartCamDevice._update_children_info = _smartcam_update_children_info
