import time
import tweepy
import logging

from src.prediction import CaptionPredictor, load_pil_image
from src.state import init_state, save_state
from src.utils import setup_logging
from src.settings import settings


logger = logging.getLogger(__name__)


def tweet_has_photo(tweet):
    if "media" in tweet.entities:
        media = tweet.entities["media"][0]
        if media["type"] == "photo":
            return True
    return False


def get_photo_urls(tweet):
    photo_urls = []
    for media in tweet.extended_entities["media"]:
        if media["type"] == "photo":
            photo_urls.append(media["media_url_https"])
    return photo_urls


def tweet_is_reply(tweet):
    return tweet.in_reply_to_status_id is not None


def tweet_text_to(api, tweet, text):
    logger.info(f"Tweet to {tweet.id}: {text}")
    try:
        tweet = api.update_status(
            status=text,
            in_reply_to_status_id=tweet.id,
            auto_populate_reply_metadata=True,
        )
        logger.info(f"New tweet id '{tweet.id}'")
    except tweepy.TweepError as error:
        logger.error(f"Raised Tweep error: {error}")
        raise error
    return tweet


def predict_and_post_captions(api, predictor, photo_urls, tweet_to_reply, mention_name):
    text_lst = []
    if mention_name:
        text_lst.append(f"@{mention_name},")
        logger.info(f"Add mention of user '{mention_name}'")

    # Generate caption for each photo
    for num, photo_url in enumerate(photo_urls):
        image = load_pil_image(photo_url)
        caption = predictor.get_captions(image)[0]
        num = f" {num + 1}" if len(photo_urls) > 1 else ""
        photo_caption_text = f"Photo{num} may show: {caption.capitalize()}."
        text_lst.append(photo_caption_text[: settings.twitter_char_limit])
        logger.info(f"Tweet '{tweet_to_reply.id}' - {photo_caption_text}")

    text = ""
    # Chunk large text into several tweets
    for num, line in enumerate(text_lst):
        if len(text) + len(line) >= settings.twitter_char_limit:
            tweet_to_reply = tweet_text_to(api, tweet_to_reply, text)
            text = ""
        if num:
            text += "\n"
        text += line
    if text:
        tweet_text_to(api, tweet_to_reply, text)


class ImageCaptioningProcessor:
    def __init__(self, api, predictor, state_path="", sleep=15.0):
        self.api = api
        self.predictor = predictor
        self.state_path = state_path
        self.sleep = sleep
        self.me = api.me()
        self.state = init_state(api, state_path)

    def process_tweet(self, tweet):
        logger.info(f"Start processing tweet '{tweet.id}'")
        if tweet.user.id == self.me.id:
            logger.info(f"Skip tweet by me '{tweet.user.id}'")
            return

        mention_name = ""
        photo_urls = []
        if tweet_has_photo(tweet):
            photo_urls = get_photo_urls(tweet)
            logger.info(f"Tweet '{tweet.id}' has photos: {photo_urls}")
        elif tweet_is_reply(tweet):
            mention_name = tweet.user.screen_name
            tweet = self.api.get_status(tweet.in_reply_to_status_id)
            if tweet_has_photo(tweet):
                photo_urls = get_photo_urls(tweet)
                logger.info(f"Replied tweet '{tweet.id}' has photos: {photo_urls}")

        if photo_urls:
            predict_and_post_captions(
                self.api, self.predictor, photo_urls, tweet, mention_name
            )
        logger.info(f"Finish processing tweet '{tweet.id}'")

    def process_mentions(self):
        logger.info(f"Retrieving mentions since_id '{self.state.since_id}'")
        for tweet in tweepy.Cursor(
            self.api.mentions_timeline, since_id=self.state.since_id
        ).items():
            try:
                self.state.since_id = max(tweet.id, self.state.since_id)
                self.process_tweet(tweet)
                save_state(self.state, self.state_path)
            except BaseException as error:
                logger.info(f"Error while processing tweet '{tweet.id}': {error}")

    def process(self):
        logger.info(f"Starting with since_id: '{self.state.since_id}'")
        while True:
            self.process_mentions()
            logger.info(f"Waiting {self.sleep} seconds")
            time.sleep(self.sleep)


if __name__ == "__main__":
    setup_logging(settings.log_level)

    auth = tweepy.OAuthHandler(settings.consumer_key, settings.consumer_secret)
    auth.set_access_token(settings.access_token, settings.access_token_secret)
    twitter_api = tweepy.API(
        auth, wait_on_rate_limit=True, wait_on_rate_limit_notify=True
    )
    twitter_api.verify_credentials()
    logger.info("Credentials verified")

    predictor_params = {
        "feature_checkpoint_path": settings.feature_checkpoint_path,
        "feature_config_path": settings.feature_config_path,
        "caption_checkpoint_path": settings.caption_checkpoint_path,
        "caption_config_path": settings.caption_config_path,
        "beam_size": 5,
        "sample_n": 1,
        "device": settings.device,
    }
    caption_predictor = CaptionPredictor(**predictor_params)
    logger.info(f"Predictor loaded with params: {predictor_params}")

    processor = ImageCaptioningProcessor(
        twitter_api, caption_predictor, state_path=settings.state_path
    )
    processor.process()
