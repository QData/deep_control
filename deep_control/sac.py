import argparse
import copy
import time
import math

import gym
import numpy as np
import tensorboardX
import torch
import torch.nn.functional as F
import tqdm

from . import envs, replay, run, utils

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")


def sac(
    agent,
    env,
    buffer,
    num_steps=1_000_000,
    max_episode_steps=100_000,
    batch_size=256,
    tau=0.005,
    actor_lr=1e-4,
    critic_lr=1e-3,
    alpha_lr=1e-4,
    gamma=0.99,
    eval_interval=5000,
    eval_episodes=10,
    warmup_steps=1000,
    actor_clip=None,
    critic_clip=None,
    actor_l2=0.0,
    critic_l2=0.0,
    delay=1,
    save_interval=100_000,
    name="sac_run",
    render=False,
    save_to_disk=True,
    log_to_disk=True,
    verbosity=0,
    gradient_updates_per_step=1,
    init_alpha=0.1,
    **kwargs,
):
    """
    Train `agent` on `env` with Soft Actor Critic algorithm.

    Reference: https://arxiv.org/abs/1801.01290 and https://arxiv.org/abs/1812.05905
    """
    agent.to(device)
    max_act = env.action_space.high[0]

    # initialize target networks
    target_agent = copy.deepcopy(agent)
    target_agent.to(device)
    utils.hard_update(target_agent.actor, agent.actor)
    utils.hard_update(target_agent.critic1, agent.critic1)
    utils.hard_update(target_agent.critic2, agent.critic2)

    critic1_optimizer = torch.optim.Adam(
        agent.critic1.parameters(), lr=critic_lr, weight_decay=critic_l2
    )
    critic2_optimizer = torch.optim.Adam(
        agent.critic2.parameters(), lr=critic_lr, weight_decay=critic_l2
    )
    actor_optimizer = torch.optim.Adam(
        agent.actor.parameters(), lr=actor_lr, weight_decay=actor_l2
    )

    log_alpha = torch.Tensor([math.log(init_alpha)]).to(device)
    log_alpha.requires_grad = True

    log_alpha_optimizer = torch.optim.Adam([log_alpha], lr=alpha_lr)

    if save_to_disk or log_to_disk:
        save_dir = utils.make_process_dirs(name)
    if log_to_disk:
        # create tb writer, save hparams
        writer = tensorboardX.SummaryWriter(save_dir)

    run.warmup_buffer(buffer, env, warmup_steps, max_episode_steps)

    done = True
    learning_curve = []

    steps_iter = range(num_steps)
    if verbosity:
        steps_iter = tqdm.tqdm(steps_iter)

    for step in steps_iter:
        if done:
            state = env.reset()
            steps_this_ep = 0
            done = False
        action, _ = agent.stochastic_forward(
            state, track_gradients=False, process_states=True
        )
        next_state, reward, done, info = env.step(action)
        buffer.push(state, action, reward, next_state, done)
        state = next_state
        steps_this_ep += 1
        if steps_this_ep >= max_episode_steps:
            done = True

        update_policy = step % delay == 0
        for _ in range(gradient_updates_per_step):
            learn(
                buffer=buffer,
                target_agent=target_agent,
                agent=agent,
                actor_optimizer=actor_optimizer,
                critic1_optimizer=critic1_optimizer,
                critic2_optimizer=critic2_optimizer,
                log_alpha=log_alpha,
                log_alpha_optimizer=log_alpha_optimizer,
                target_entropy=-env.action_space.shape[0],  # target_entropy = -|A|
                batch_size=batch_size,
                gamma=gamma,
                critic_clip=critic_clip,
                actor_clip=actor_clip,
                update_policy=update_policy,
            )

            # move target model towards training model
            if update_policy:
                utils.soft_update(target_agent.actor, agent.actor, tau)
                utils.soft_update(target_agent.critic1, agent.critic1, tau)
                utils.soft_update(target_agent.critic2, agent.critic2, tau)

        if step % eval_interval == 0 or step == num_steps - 1:
            mean_return = run.evaluate_agent(
                agent, env, eval_episodes, max_episode_steps, render
            )
            if log_to_disk:
                writer.add_scalar("return", mean_return, step)

            learning_curve.append((step, mean_return))

        if step % save_interval == 0 and save_to_disk:
            agent.save(save_dir)

    if save_to_disk:
        agent.save(save_dir)
    return agent


def learn(
    buffer,
    target_agent,
    agent,
    actor_optimizer,
    critic1_optimizer,
    critic2_optimizer,
    log_alpha_optimizer,
    target_entropy,
    batch_size,
    log_alpha,
    gamma,
    critic_clip,
    actor_clip,
    update_policy=True,
):
    per = isinstance(buffer, replay.PrioritizedReplayBuffer)
    if per:
        batch, imp_weights, priority_idxs = buffer.sample(batch_size)
        imp_weights = imp_weights.to(device)
    else:
        batch = buffer.sample(batch_size)

    # prepare transitions for models
    state_batch, action_batch, reward_batch, next_state_batch, done_batch = batch
    state_batch = state_batch.to(device)
    next_state_batch = next_state_batch.to(device)
    action_batch = action_batch.to(device)
    reward_batch = reward_batch.to(device)
    done_batch = done_batch.to(device)

    agent.train()

    alpha = torch.exp(log_alpha)
    with torch.no_grad():
        # create critic targets (clipped double Q learning)
        action_s2, logp_a2 = agent.stochastic_forward(
            next_state_batch, track_gradients=False, process_states=False
        )
        target_action_value_s2 = torch.min(
            target_agent.critic1(next_state_batch, action_s2),
            target_agent.critic2(next_state_batch, action_s2),
        )
        td_target = reward_batch + gamma * (1.0 - done_batch) * (
            target_action_value_s2 - (alpha * logp_a2)
        )

    # update first critic
    agent_critic1_pred = agent.critic1(state_batch, action_batch)
    td_error1 = td_target - agent_critic1_pred
    if per:
        critic1_loss = (imp_weights * 0.5 * (td_error1 ** 2)).mean()
    else:
        critic1_loss = 0.5 * (td_error1 ** 2).mean()
    critic1_optimizer.zero_grad()
    critic1_loss.backward()
    if critic_clip:
        torch.nn.utils.clip_grad_norm_(agent.critic1.parameters(), critic_clip)
    critic1_optimizer.step()

    # update second critic
    agent_critic2_pred = agent.critic2(state_batch, action_batch)
    td_error2 = td_target - agent_critic2_pred
    if per:
        critic2_loss = (imp_weights * 0.5 * (td_error2 ** 2)).mean()
    else:
        critic2_loss = 0.5 * (td_error2 ** 2).mean()
    critic2_optimizer.zero_grad()
    critic2_loss.backward()
    if critic_clip:
        torch.nn.utils.clip_grad_norm_(agent.critic2.parameters(), critic_clip)
    critic2_optimizer.step()

    if update_policy:
        # actor update
        agent_actions, logp_a = agent.stochastic_forward(
            state_batch, track_gradients=True, process_states=False
        )
        actor_loss = -(
            torch.min(
                agent.critic1(state_batch, agent_actions),
                agent.critic2(state_batch, agent_actions),
            )
            - (alpha.detach() * logp_a)
        ).mean()
        actor_optimizer.zero_grad()
        actor_loss.backward()
        if actor_clip:
            torch.nn.utils.clip_grad_norm_(agent.actor.parameters(), actor_clip)
        actor_optimizer.step()

        # alpha update
        alpha_loss = (-alpha * (logp_a + target_entropy).detach()).mean()
        log_alpha_optimizer.zero_grad()
        alpha_loss.backward()
        log_alpha_optimizer.step()

    if per:
        new_priorities = (abs(td_error1) + 1e-5).cpu().detach().squeeze(1).numpy()
        buffer.update_priorities(priority_idxs, new_priorities)


def add_args(parser):
    parser.add_argument(
        "--num_steps", type=int, default=10 ** 6, help="number of steps in training"
    )
    parser.add_argument(
        "--max_episode_steps",
        type=int,
        default=100000,
        help="maximum steps per episode",
    )
    parser.add_argument(
        "--batch_size", type=int, default=256, help="training batch size"
    )
    parser.add_argument(
        "--tau", type=float, default=0.005, help="for model parameter % update"
    )
    parser.add_argument(
        "--actor_lr", type=float, default=1e-4, help="actor learning rate"
    )
    parser.add_argument(
        "--critic_lr", type=float, default=1e-3, help="critic learning rate"
    )
    parser.add_argument(
        "--gamma", type=float, default=0.99, help="gamma, the discount factor"
    )
    parser.add_argument(
        "--init_alpha",
        type=float,
        default=0.1,
        help="initial entropy regularization coefficeint.",
    )
    parser.add_argument(
        "--alpha_lr",
        type=float,
        default=1e-4,
        help="alpha (entropy regularization coefficeint) learning rate",
    )
    parser.add_argument(
        "--buffer_size", type=int, default=1_000_000, help="replay buffer size"
    )
    parser.add_argument(
        "--eval_interval",
        type=int,
        default=5000,
        help="how often to test the agent without exploration (in episodes)",
    )
    parser.add_argument(
        "--eval_episodes",
        type=int,
        default=10,
        help="how many episodes to run for when testing",
    )
    parser.add_argument(
        "--warmup_steps", type=int, default=1000, help="warmup length, in steps"
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="flag to enable env rendering during training",
    )
    parser.add_argument(
        "--actor_clip",
        type=float,
        default=None,
        help="gradient clipping for actor updates",
    )
    parser.add_argument(
        "--critic_clip",
        type=float,
        default=None,
        help="gradient clipping for critic updates",
    )
    parser.add_argument(
        "--name", type=str, default="sac_run", help="dir name for saves"
    )
    parser.add_argument(
        "--actor_l2",
        type=float,
        default=0.0,
        help="L2 regularization coeff for actor network",
    )
    parser.add_argument(
        "--critic_l2",
        type=float,
        default=0.0,
        help="L2 regularization coeff for critic network",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=1,
        help="How many steps to go between actor updates",
    )
    parser.add_argument(
        "--save_interval",
        type=int,
        default=100_000,
        help="How many steps to go between saving the agent params to disk",
    )
    parser.add_argument(
        "--verbosity",
        type=int,
        default=1,
        help="verbosity > 0 displays a progress bar during training",
    )
    parser.add_argument(
        "--gradient_updates_per_step",
        type=int,
        default=1,
        help="how many gradient updates to make per env step",
    )
    parser.add_argument(
        "--prioritized_replay",
        action="store_true",
        help="flag that enables use of prioritized experience replay",
    )
    parser.add_argument(
        "--skip_save_to_disk",
        action="store_true",
        help="flag to skip saving agent params to disk during training",
    )
    parser.add_argument(
        "--skip_log_to_disk",
        action="store_true",
        help="flag to skip saving agent performance logs to disk during training",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--env", type=str, default="Pendulum-v0", help="Training environment gym id"
    )
    add_args(parser)
    args = parser.parse_args()
    agent, env = envs.load_env(args.env, "sac")

    if args.prioritized_replay:
        buffer_t = replay.PrioritizedReplayBuffer
    else:
        buffer_t = replay.ReplayBuffer
    buffer = buffer_t(
        args.buffer_size,
        state_shape=env.observation_space.shape,
        action_shape=env.action_space.shape,
    )

    print(f"Using Device: {device}")
    agent = sac(
        agent,
        env,
        buffer,
        num_steps=args.num_steps,
        max_episode_steps=args.max_episode_steps,
        batch_size=args.batch_size,
        tau=args.tau,
        actor_lr=args.actor_lr,
        critic_lr=args.critic_lr,
        gamma=args.gamma,
        eval_interval=args.eval_interval,
        eval_episodes=args.eval_episodes,
        warmup_steps=args.warmup_steps,
        actor_clip=args.actor_clip,
        critic_clip=args.critic_clip,
        actor_l2=args.actor_l2,
        critic_l2=args.critic_l2,
        init_alpha=args.init_alpha,
        alpha_lr=args.alpha_lr,
        delay=args.delay,
        save_interval=args.save_interval,
        name=args.name,
        render=args.render,
        verbosity=args.verbosity,
        gradient_updates_per_step=args.gradient_updates_per_step,
        save_to_disk=not args.skip_save_to_disk,
        log_to_disk=not args.skip_log_to_disk,
    )
