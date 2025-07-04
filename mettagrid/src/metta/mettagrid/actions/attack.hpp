#ifndef ACTIONS_ATTACK_HPP_
#define ACTIONS_ATTACK_HPP_

#include <string>
#include <vector>

#include "action_handler.hpp"
#include "grid_object.hpp"
#include "objects/agent.hpp"
#include "objects/constants.hpp"
#include "objects/metta_object.hpp"
#include "types.hpp"

class Attack : public ActionHandler {
public:
  explicit Attack(const ActionConfig& cfg,
                  const std::map<InventoryItem, int>& attack_resources,
                  const std::map<InventoryItem, int>& defense_resources,
                  const std::string& action_name = "attack")
      : ActionHandler(cfg, action_name), _attack_resources(attack_resources), _defense_resources(defense_resources) {
    priority = 1;
  }

  unsigned char max_arg() const override {
    return 9;
  }

protected:
  std::map<InventoryItem, int> _attack_resources;
  std::map<InventoryItem, int> _defense_resources;

  bool _handle_action(Agent* actor, ActionArg arg) override {
    if (arg > 9 || arg < 1) {
      return false;
    }

    for (const auto& [item, amount] : _attack_resources) {
      if (actor->inventory[item] < amount) {
        return false;
      }
    }

    for (const auto& [item, amount] : _attack_resources) {
      int used_amount = std::abs(actor->update_inventory(item, -amount));
      assert(used_amount == amount);
    }

    short distance = 1 + (arg - 1) / 3;
    short offset = -((arg - 1) % 3 - 1);

    GridLocation target_loc =
        _grid->relative_location(actor->location, static_cast<Orientation>(actor->orientation), distance, offset);

    return _handle_target(actor, target_loc);
  }

  bool _handle_target(Agent* actor, GridLocation target_loc) {
    target_loc.layer = GridLayer::Agent_Layer;
    Agent* agent_target = static_cast<Agent*>(_grid->object_at(target_loc));

    bool was_frozen = false;
    if (agent_target) {
      // Track attack targets
      actor->stats.incr("action." + _action_name + "." + agent_target->type_name);
      actor->stats.incr("action." + _action_name + "." + agent_target->type_name + "." + actor->group_name);
      actor->stats.incr("action." + _action_name + "." + agent_target->type_name + "." + actor->group_name + "." +
                        agent_target->group_name);

      if (agent_target->group_name == actor->group_name) {
        actor->stats.incr("attack.own_team." + actor->group_name);
      } else {
        actor->stats.incr("attack.other_team." + actor->group_name);
      }

      was_frozen = agent_target->frozen > 0;

      bool blocked = _defense_resources.size() > 0;
      for (const auto& [item, amount] : _defense_resources) {
        if (agent_target->inventory[item] < amount) {
          blocked = false;
          break;
        }
      }

      if (blocked) {
        // Consume the defense resources
        for (const auto& [item, amount] : _defense_resources) {
          int used_amount = std::abs(agent_target->update_inventory(item, -amount));
          assert(used_amount == amount);
        }

        actor->stats.incr("attack.blocked." + agent_target->group_name);
        actor->stats.incr("attack.blocked." + agent_target->group_name + "." + actor->group_name);
        return true;
      } else {
        agent_target->frozen = agent_target->freeze_duration;

        if (!was_frozen) {
          // Actor (attacker) stats
          actor->stats.incr("attack.win." + actor->group_name);
          actor->stats.incr("attack.win." + actor->group_name + "." + agent_target->group_name);

          // Target (victim) stats - these should be on agent_target, not actor
          agent_target->stats.incr("attack.loss." + agent_target->group_name);
          agent_target->stats.incr("attack.loss." + agent_target->group_name + "." + actor->group_name);

          if (agent_target->group_name == actor->group_name) {
            actor->stats.incr("attack.win.own_team." + actor->group_name);
            agent_target->stats.incr("attack.loss.from_own_team." + agent_target->group_name);
          } else {
            actor->stats.incr("attack.win.other_team." + actor->group_name);
            agent_target->stats.incr("attack.loss.from_other_team." + agent_target->group_name);
          }

          // Collect all items to steal first, then apply changes, since the changes
          // can delete keys from the agent's inventory.
          std::vector<std::pair<InventoryItem, int>> items_to_steal;
          for (const auto& [item, amount] : agent_target->inventory) {
            items_to_steal.emplace_back(item, amount);
          }

          // Now apply the stealing
          for (const auto& [item, amount] : items_to_steal) {
            int stolen = actor->update_inventory(item, amount);

            agent_target->update_inventory(item, -stolen);
            if (stolen > 0) {
              actor->stats.add(actor->stats.inventory_item_name(item) + ".stolen." + actor->group_name, stolen);
              // Also track what was stolen from the victim's perspective
              agent_target->stats.add(
                  agent_target->stats.inventory_item_name(item) + ".stolen_from." + agent_target->group_name, stolen);
            }
          }
        }

        return true;
      }
    }

    return false;
  }
};

#endif  // ACTIONS_ATTACK_HPP_
