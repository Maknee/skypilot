"""Credential checks: check cloud credentials and enable clouds."""
import collections
import itertools
import os
import traceback
from types import ModuleType
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple, Union

import click
import colorama

from sky import clouds as sky_clouds
from sky import exceptions
from sky import global_user_state
from sky import skypilot_config
from sky.adaptors import cloudflare
from sky.clouds import cloud as sky_cloud
from sky.utils import registry
from sky.utils import rich_utils
from sky.utils import subprocess_utils
from sky.utils import ux_utils

CHECK_MARK_EMOJI = '\U00002714'  # Heavy check mark unicode
PARTY_POPPER_EMOJI = '\U0001F389'  # Party popper unicode


def check_capabilities(
    quiet: bool = False,
    verbose: bool = False,
    clouds: Optional[Iterable[str]] = None,
    capabilities: Optional[List[sky_cloud.CloudCapability]] = None,
) -> Dict[str, List[sky_cloud.CloudCapability]]:
    echo = (lambda *_args, **_kwargs: None
           ) if quiet else lambda *args, **kwargs: click.echo(
               *args, **kwargs, color=True)
    echo('Checking credentials to enable infra for SkyPilot.')
    if capabilities is None:
        capabilities = sky_cloud.ALL_CAPABILITIES
    assert capabilities is not None
    enabled_clouds: Dict[str, List[sky_cloud.CloudCapability]] = {}
    disabled_clouds: Dict[str, List[sky_cloud.CloudCapability]] = {}

    def check_one_cloud_one_capability(
        payload: Tuple[Tuple[str, Union[sky_clouds.Cloud, ModuleType]],
                       sky_cloud.CloudCapability]
    ) -> Optional[Tuple[sky_cloud.CloudCapability, bool, Optional[Union[
            str, Dict[str, str]]]]]:
        cloud_tuple, capability = payload
        _, cloud = cloud_tuple
        try:
            ok, reason = cloud.check_credentials(capability)
        except exceptions.NotSupportedError:
            return None
        except Exception:  # pylint: disable=broad-except
            ok, reason = False, traceback.format_exc()
        if not isinstance(reason, dict):
            reason = reason.strip() if reason else None
        return (capability, ok, reason)

    def get_cloud_tuple(
            cloud_name: str) -> Tuple[str, Union[sky_clouds.Cloud, ModuleType]]:
        # Validates cloud_name and returns a tuple of the cloud's name and
        # the cloud object. Includes special handling for Cloudflare.
        if cloud_name.lower().startswith('cloudflare'):
            return cloudflare.NAME, cloudflare
        else:
            cloud_obj = registry.CLOUD_REGISTRY.from_str(cloud_name)
            assert cloud_obj is not None, f'Cloud {cloud_name!r} not found'
            return repr(cloud_obj), cloud_obj

    def get_all_clouds():
        return tuple([repr(c) for c in registry.CLOUD_REGISTRY.values()] +
                     [cloudflare.NAME])

    if clouds is not None:
        cloud_list = clouds
    else:
        cloud_list = get_all_clouds()
    clouds_to_check = [get_cloud_tuple(c) for c in cloud_list]

    # Use allowed_clouds from config if it exists, otherwise check all clouds.
    # Also validate names with get_cloud_tuple.
    config_allowed_cloud_names = sorted([
        get_cloud_tuple(c)[0] for c in skypilot_config.get_nested((
            'allowed_clouds',), get_all_clouds())
    ])
    # Use disallowed_cloud_names for logging the clouds that will be disabled
    # because they are not included in allowed_clouds in config.yaml.
    disallowed_cloud_names = [
        c for c in get_all_clouds() if c not in config_allowed_cloud_names
    ]
    # Check only the clouds which are allowed in the config.
    clouds_to_check = [
        c for c in clouds_to_check if c[0] in config_allowed_cloud_names
    ]

    combinations = list(itertools.product(clouds_to_check, capabilities))
    with rich_utils.safe_status('Checking infra choices...'):
        check_results = subprocess_utils.run_in_parallel(
            check_one_cloud_one_capability, combinations)

    check_results_dict: Dict[
        Tuple[str, Union[sky_clouds.Cloud, ModuleType]],
        List[Tuple[sky_cloud.CloudCapability, bool,
                   Optional[Union[str,
                                  Dict[str,
                                       str]]]]]] = collections.defaultdict(list)
    cloud2ctx2text: Dict[str, Dict[str, str]] = {}
    for combination, check_result in zip(combinations, check_results):
        if check_result is None:
            continue
        capability, ok, ctx2text = check_result
        cloud_tuple, _ = combination
        cloud_repr = cloud_tuple[0]
        if isinstance(ctx2text, dict):
            cloud2ctx2text[cloud_repr] = ctx2text
        if ok:
            enabled_clouds.setdefault(cloud_repr, []).append(capability)
        else:
            disabled_clouds.setdefault(cloud_repr, []).append(capability)
        check_results_dict[cloud_tuple].append(check_result)

    for cloud_tuple, check_result_list in sorted(check_results_dict.items(),
                                                 key=lambda item: item[0][0]):
        _print_checked_cloud(echo, verbose, cloud_tuple, check_result_list,
                             cloud2ctx2text.get(cloud_tuple[0], {}))

    # Determine the set of enabled clouds: (previously enabled clouds + newly
    # enabled clouds - newly disabled clouds) intersected with
    # config_allowed_clouds, if specified in config.yaml.
    # This means that if a cloud is already enabled and is not included in
    # allowed_clouds in config.yaml, it will be disabled.
    all_enabled_clouds: Set[str] = set()
    for capability in capabilities:
        # Cloudflare is not a real cloud in registry.CLOUD_REGISTRY, and should
        # not be inserted into the DB (otherwise `sky launch` and other code
        # would error out when it's trying to look it up in the registry).
        enabled_clouds_set = {
            cloud for cloud, capabilities in enabled_clouds.items()
            if capability in capabilities and not cloud.startswith('Cloudflare')
        }
        disabled_clouds_set = {
            cloud for cloud, capabilities in disabled_clouds.items()
            if capability in capabilities and not cloud.startswith('Cloudflare')
        }
        config_allowed_clouds_set = {
            cloud for cloud in config_allowed_cloud_names
            if not cloud.startswith('Cloudflare')
        }
        previously_enabled_clouds_set = {
            repr(cloud)
            for cloud in global_user_state.get_cached_enabled_clouds(capability)
        }
        enabled_clouds_for_capability = (config_allowed_clouds_set & (
            (previously_enabled_clouds_set | enabled_clouds_set) -
            disabled_clouds_set))
        global_user_state.set_enabled_clouds(
            list(enabled_clouds_for_capability), capability)
        all_enabled_clouds = all_enabled_clouds.union(
            enabled_clouds_for_capability)
    disallowed_clouds_hint = None
    if disallowed_cloud_names:
        disallowed_clouds_hint = (
            '\nNote: The following clouds were disabled because they were not '
            'included in allowed_clouds in ~/.sky/config.yaml: '
            f'{", ".join([c for c in disallowed_cloud_names])}')
    if not all_enabled_clouds:
        echo(
            click.style(
                'No cloud is enabled. SkyPilot will not be able to run any '
                'task. Run `sky check` for more info.',
                fg='red',
                bold=True))
        if disallowed_clouds_hint:
            echo(click.style(disallowed_clouds_hint, dim=True))
        raise SystemExit()
    else:
        clouds_arg = (f' {" ".join(disabled_clouds).lower()}'
                      if clouds is not None else '')
        echo(
            click.style(
                '\nTo enable a cloud, follow the hints above and rerun: ',
                dim=True) + click.style(f'sky check{clouds_arg}', bold=True) +
            '\n' + click.style(
                'If any problems remain, refer to detailed docs at: '
                'https://docs.skypilot.co/en/latest/getting-started/installation.html',  # pylint: disable=line-too-long
                dim=True))

        if disallowed_clouds_hint:
            echo(click.style(disallowed_clouds_hint, dim=True))

        # Pretty print for UX.
        if not quiet:
            enabled_clouds_str = '\n  ' + '\n  '.join([
                _format_enabled_cloud(cloud, capabilities,
                                      cloud2ctx2text.get(cloud, None))
                for cloud, capabilities in sorted(enabled_clouds.items(),
                                                  key=lambda item: item[0])
            ])
            echo(f'\n{colorama.Fore.GREEN}{PARTY_POPPER_EMOJI} '
                 f'Enabled infra {PARTY_POPPER_EMOJI}'
                 f'{colorama.Style.RESET_ALL}{enabled_clouds_str}')
    return enabled_clouds


def check_capability(
    capability: sky_cloud.CloudCapability,
    quiet: bool = False,
    verbose: bool = False,
    clouds: Optional[Iterable[str]] = None,
) -> List[str]:
    clouds_with_capability = []
    enabled_clouds = check_capabilities(quiet, verbose, clouds, [capability])
    for cloud, capabilities in enabled_clouds.items():
        if capability in capabilities:
            clouds_with_capability.append(cloud)
    return clouds_with_capability


def check(
    quiet: bool = False,
    verbose: bool = False,
    clouds: Optional[Iterable[str]] = None,
) -> List[str]:
    return list(
        check_capabilities(quiet, verbose, clouds,
                           sky_cloud.ALL_CAPABILITIES).keys())


def get_cached_enabled_clouds_or_refresh(
        capability: sky_cloud.CloudCapability,
        raise_if_no_cloud_access: bool = False) -> List[sky_clouds.Cloud]:
    """Returns cached enabled clouds and if no cloud is enabled, refresh.

    This function will perform a refresh if no public cloud is enabled.

    Args:
        raise_if_no_cloud_access: if True, raise an exception if no public
            cloud is enabled.

    Raises:
        exceptions.NoCloudAccessError: if no public cloud is enabled and
            raise_if_no_cloud_access is set to True.
    """
    cached_enabled_clouds = global_user_state.get_cached_enabled_clouds(
        capability)
    if not cached_enabled_clouds:
        try:
            check_capability(sky_cloud.CloudCapability.COMPUTE, quiet=True)
        except SystemExit:
            # If no cloud is enabled, check() will raise SystemExit.
            # Here we catch it and raise the exception later only if
            # raise_if_no_cloud_access is set to True.
            pass
        cached_enabled_clouds = global_user_state.get_cached_enabled_clouds(
            capability)
    if raise_if_no_cloud_access and not cached_enabled_clouds:
        with ux_utils.print_exception_no_traceback():
            raise exceptions.NoCloudAccessError(
                'Cloud access is not set up. Run: '
                f'{colorama.Style.BRIGHT}sky check{colorama.Style.RESET_ALL}')
    return cached_enabled_clouds


def get_cloud_credential_file_mounts(
        excluded_clouds: Optional[Iterable[sky_clouds.Cloud]]
) -> Dict[str, str]:
    """Returns the files necessary to access all clouds.

    Returns a dictionary that will be added to a task's file mounts
    and a list of patterns that will be excluded (used as rsync_exclude).
    """
    # Uploading credentials for all clouds instead of only sky check
    # enabled clouds because users may have partial credentials for some
    # clouds to access their specific resources (e.g. cloud storage) but
    # not have the complete credentials to pass sky check.
    clouds = registry.CLOUD_REGISTRY.values()
    file_mounts = {}
    for cloud in clouds:
        if (excluded_clouds is not None and
                sky_clouds.cloud_in_iterable(cloud, excluded_clouds)):
            continue
        cloud_file_mounts = cloud.get_credential_file_mounts()
        for remote_path, local_path in cloud_file_mounts.items():
            if os.path.exists(os.path.expanduser(local_path)):
                file_mounts[remote_path] = local_path
    # Currently, get_cached_enabled_clouds_or_refresh() does not support r2 as
    # only clouds with computing instances are marked as enabled by skypilot.
    # This will be removed when cloudflare/r2 is added as a 'cloud'.
    r2_is_enabled, _ = cloudflare.check_storage_credentials()
    if r2_is_enabled:
        r2_credential_mounts = cloudflare.get_credential_file_mounts()
        file_mounts.update(r2_credential_mounts)
    return file_mounts


def _print_checked_cloud(
    echo: Callable,
    verbose: bool,
    cloud_tuple: Tuple[str, Union[sky_clouds.Cloud, ModuleType]],
    cloud_capabilities: List[Tuple[sky_cloud.CloudCapability, bool,
                                   Optional[Union[str, Dict[str, str]]]]],
    ctx2text: Dict[str, str],
) -> None:
    """Prints whether a cloud is enabled, and the capabilities that are enabled.
    If any hints (for enabled capabilities) or
    reasons (for disabled capabilities) are provided, they will be printed.

    Args:
        echo: The function to use to print the message.
        verbose: Whether to print the verbose output.
        cloud_tuple: The cloud to print the capabilities for.
        cloud_capabilities: The capabilities for the cloud.
    """

    def _yellow_color(str_to_format: str) -> str:
        return (f'{colorama.Fore.LIGHTYELLOW_EX}'
                f'{str_to_format}'
                f'{colorama.Style.RESET_ALL}')

    cloud_repr, cloud = cloud_tuple
    # Print the capabilities for the cloud.
    # consider cloud enabled if any capability is enabled.
    enabled_capabilities: List[sky_cloud.CloudCapability] = []
    hints_to_capabilities: Dict[str, List[sky_cloud.CloudCapability]] = {}
    reasons_to_capabilities: Dict[str, List[sky_cloud.CloudCapability]] = {}
    for capability, ok, reason in cloud_capabilities:
        if ok:
            enabled_capabilities.append(capability)
        # `dict` reasons for K8s and SSH will be printed in detail in
        # _format_enabled_cloud. Skip here.
        if not isinstance(reason, str):
            continue
        if ok:
            if reason is not None:
                hints_to_capabilities.setdefault(reason, []).append(capability)
        elif reason is not None:
            reasons_to_capabilities.setdefault(reason, []).append(capability)
    style_str = f'{colorama.Style.DIM}'
    status_msg: str = 'disabled'
    capability_string: str = ''
    detail_string: str = ''
    activated_account: Optional[str] = None
    if enabled_capabilities:
        style_str = f'{colorama.Fore.GREEN}{colorama.Style.NORMAL}'
        status_msg = 'enabled'
        capability_string = f'[{", ".join(enabled_capabilities)}]'
        if verbose and cloud is not cloudflare:
            activated_account = cloud.get_active_user_identity_str()
        if isinstance(cloud_tuple[1], (sky_clouds.SSH, sky_clouds.Kubernetes)):
            detail_string = _format_context_details(cloud_tuple[1],
                                                    show_details=True,
                                                    ctx2text=ctx2text)
    echo(
        click.style(
            f'{style_str}  {cloud_repr}: {status_msg} {capability_string}'
            f'{colorama.Style.RESET_ALL}{detail_string}'))
    if activated_account is not None:
        echo(f'    Activated account: {activated_account}')
    for reason, caps in hints_to_capabilities.items():
        echo(f'    Hint [{", ".join(caps)}]: {_yellow_color(reason)}')
    for reason, caps in reasons_to_capabilities.items():
        echo(f'    Reason [{", ".join(caps)}]: {reason}')


def _green_color(str_to_format: str) -> str:
    return f'{colorama.Fore.GREEN}{str_to_format}{colorama.Style.RESET_ALL}'


def _format_context_details(cloud: Union[str, sky_clouds.Cloud],
                            show_details: bool,
                            ctx2text: Optional[Dict[str, str]] = None) -> str:
    if isinstance(cloud, str):
        cloud_type = registry.CLOUD_REGISTRY.from_str(cloud)
        assert cloud_type is not None
    else:
        cloud_type = cloud
    if isinstance(cloud_type, sky_clouds.SSH):
        # Get the cluster names by reading from the node pools file
        contexts = sky_clouds.SSH.get_ssh_node_pool_contexts()
    else:
        assert isinstance(cloud_type, sky_clouds.Kubernetes)
        contexts = sky_clouds.Kubernetes.existing_allowed_contexts()

    filtered_contexts = []
    for context in contexts:
        if not show_details:
            # Skip
            if (ctx2text is None or context not in ctx2text or
                    'disabled' in ctx2text[context]):
                continue
        filtered_contexts.append(context)

    if not filtered_contexts:
        return ''

    # Format the context info with consistent styling
    contexts_formatted = []
    for i, context in enumerate(filtered_contexts):
        if isinstance(cloud_type, sky_clouds.SSH):
            # TODO: This is a hack to remove the 'ssh-' prefix from the
            # context name. Once we have a separate kubeconfig for SSH,
            # this will not be required.
            cleaned_context = context.lstrip('ssh-')
        else:
            cleaned_context = context
        symbol = (ux_utils.INDENT_LAST_SYMBOL if i == len(filtered_contexts) -
                  1 else ux_utils.INDENT_SYMBOL)
        text_suffix = ''
        if show_details:
            if ctx2text is not None:
                text_suffix = (
                    f': {ctx2text[context]}' if context in ctx2text else ': ' +
                    'disabled. Reason: Not set up. '
                    f'Use `sky ssh up --infra {context.lstrip("ssh-")}` to '
                    'set up.')
        contexts_formatted.append(
            f'\n    {symbol}{cleaned_context}{text_suffix}')
    identity_str = ('SSH Node Pools' if isinstance(cloud_type, sky_clouds.SSH)
                    else 'Allowed contexts')
    return f'\n    {identity_str}:{"".join(contexts_formatted)}'


def _format_enabled_cloud(cloud_name: str,
                          capabilities: List[sky_cloud.CloudCapability],
                          ctx2text: Optional[Dict[str, str]] = None) -> str:
    """Format the summary of enabled cloud and its enabled capabilities.

    Args:
        cloud_name: The name of the cloud.
        capabilities: The capabilities of the cloud.

    Returns:
        A string of the formatted cloud and capabilities.
    """
    cloud_and_capabilities = f'{cloud_name} [{", ".join(capabilities)}]'
    title = _green_color(cloud_and_capabilities)

    if cloud_name in [repr(sky_clouds.Kubernetes()), repr(sky_clouds.SSH())]:
        return (f'{title}' + _format_context_details(
            cloud_name, show_details=False, ctx2text=ctx2text))

    return title
