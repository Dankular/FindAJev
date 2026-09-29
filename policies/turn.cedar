// Turn-taking: may the assistant take the floor? Inputs: the text detector's P(complete) (turn/detector.py, 0-100), silence duration, voice activity, and an
// independent rule ("dangling": the last word is a connective/article, so the sentence cannot be finished). `forbid` wins.

@id("turn.respond.confident")
permit (principal, action == Action::"Respond", resource)
when { context.p_complete >= {{turn_min_p}} && context.silence_ms >= {{turn_min_silence_ms}} };

// never wait forever on a low-probability turn
@id("turn.respond.timeout")
permit (principal, action == Action::"Respond", resource)
when { context.silence_ms >= {{turn_max_wait_ms}} };

@id("turn.forbid.barge-in")
forbid (principal, action == Action::"Respond", resource)
when { context.user_speaking };

// cross-check: the detector says done but the sentence ends on a dangling word -> only a long silence may override
@id("turn.forbid.dangling-early")
forbid (principal, action == Action::"Respond", resource)
when { context.dangling && context.silence_ms < {{turn_max_wait_ms}} };

@id("turn.wait.default")
permit (principal, action == Action::"Wait", resource);

// a short acknowledgement while the turn is probably not finished but the speaker paused
@id("turn.backchannel.pause")
permit (principal, action == Action::"Backchannel", resource)
when { context.p_complete < {{turn_min_p}} && context.p_complete >= {{turn_backchannel_p}} && context.silence_ms >= {{turn_min_silence_ms}} };

@id("turn.forbid.backchannel-over-speech")
forbid (principal, action == Action::"Backchannel", resource)
when { context.user_speaking };
