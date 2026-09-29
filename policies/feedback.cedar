// Sentiment + product feedback triage.

@id("review.thank.permit")
permit (principal, action == Action::"AutoThank", resource)
when { ["review_sentiment", "restaurant_review"].contains(context.domain)
    && context has review_sentiment && context.review_sentiment == "positive" };

@id("review.escalate.permit")
permit (principal, action == Action::"EscalateHuman", resource)
when { ["review_sentiment", "restaurant_review"].contains(context.domain)
    && context has review_sentiment && context.review_sentiment == "negative" };

@id("feedback.bug.permit")
permit (principal, action == Action::"FileBug", resource)
when { context.domain == "product_feedback" && context has feedback_type && context.feedback_type == "bug" };

@id("feedback.reply.permit")
permit (principal, action == Action::"AutoReply", resource)
when { context.domain == "product_feedback" && context has feedback_type
    && ["how_to", "praise"].contains(context.feedback_type) };
