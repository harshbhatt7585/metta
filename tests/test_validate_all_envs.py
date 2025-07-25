from typing import Any, cast

import pytest
from botocore.exceptions import NoCredentialsError, ProfileNotFound
from omegaconf import OmegaConf

import metta.mettagrid.mettagrid_c_config
from metta.common.util.mettagrid_cfgs import MettagridCfgFileMetadata
from metta.common.util.resolvers import register_resolvers
from metta.map.utils.storable_map import map_builder_cfg_to_storable_map

register_resolvers()


def map_or_env_configs() -> list[MettagridCfgFileMetadata]:
    metadata_by_kind = MettagridCfgFileMetadata.get_all()
    result = metadata_by_kind["map"] + metadata_by_kind["env"]

    # If this test is failing and you have configs that are too hard to fix
    # properly, you can add them to this list.
    exclude_patterns = [
        "multiagent/experiments",
        "multiagent/multiagent",
        "mettagrid.yaml",
        # usually incomplete
        "defaults",
        # partials
        "game/agent",
        "game/groups",
        "game/objects",
        "game/reward_sharing",
        # have unset params
        "game/map_builder/load.yaml",
        "game/map_builder/load_random.yaml",
    ]

    # exclude some configs that won't work
    result = [cfg for cfg in result if not any(pattern in cfg.path for pattern in exclude_patterns)]

    return result


@pytest.mark.parametrize("cfg_metadata", map_or_env_configs(), ids=[cfg.path for cfg in map_or_env_configs()])
class TestValidateAllEnvs:
    def test_map(self, cfg_metadata):
        try:
            map_cfg = cfg_metadata.get_cfg().get_map_cfg()
            map_builder_cfg_to_storable_map(map_cfg)
        except (NoCredentialsError, ProfileNotFound) as e:
            pytest.skip(f"Skipping {cfg_metadata.path} because it requires AWS credentials: {e}")
        except Exception as e:
            pytest.fail(f"Failed to validate map config {cfg_metadata.path}: {e}")

    def test_mettagrid_config(self, cfg_metadata):
        if cfg_metadata.kind != "env":
            return

        cfg = cfg_metadata.get_cfg().cfg.game
        OmegaConf.resolve(cfg)
        game_config_dict = OmegaConf.to_container(cfg, resolve=True)
        assert isinstance(game_config_dict, dict)
        del game_config_dict["map_builder"]

        # uncomment for debugging
        print(OmegaConf.to_yaml(OmegaConf.create(game_config_dict)))

        metta.mettagrid.mettagrid_c_config.from_mettagrid_config(cast(dict[str, Any], game_config_dict))
