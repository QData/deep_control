import argparse

import deep_control as dc


def train_dmc_mbpo(args):
    train_env = dc.envs.load_dmc(**vars(args))
    test_env = dc.envs.load_dmc(**vars(args))

    obs_shape = train_env.observation_space.shape
    action_shape = train_env.action_space.shape
    max_action = train_env.action_space.high[0]

    agent = dc.sac.SACAgent(
        obs_shape[0], action_shape[0], args.log_std_low, args.log_std_high
    )

    # select a replay buffer
    if args.prioritized_replay:
        buffer_t = dc.replay.PrioritizedReplayBuffer
    else:
        buffer_t = dc.replay.ReplayBuffer
    buffer = buffer_t(
        args.buffer_size,
        state_dtype=float,
        state_shape=train_env.observation_space.shape,
        action_shape=train_env.action_space.shape,
    )

    modelCls = eval(f"dc.models.{args.modelCls}")
    del args.modelCls

    agent = dc.mbpo.mbpo(
        agent=agent,
        train_env=train_env,
        test_env=test_env,
        buffer=buffer,
        modelCls=modelCls,
        **vars(args),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # add dmc-related cl args
    dc.envs.add_dmc_args(parser)
    # add mbpo-related cl args
    dc.mbpo.add_args(parser)
    args = parser.parse_args()
    args.from_pixels = False
    args.max_episode_steps = 1000
    train_dmc_mbpo(args)
