
import enum
from collections import defaultdict

from django.db import models
from django.utils.translation import ugettext as _
from django.core.exceptions import ObjectDoesNotExist

from dixit import settings
from dixit.game.exceptions import GameDeckExhausted, GameRoundIncomplete


class GameStatus(enum.Enum):
    NEW = 'new'
    ONGOING = 'ongoing'
    FINISHED = 'finished'
    ABANDONED = 'abandoned'


class Game(models.Model):
    """
    Describes a Dixit game.

    All games have an owner and are created as new. When a game is created
    the owner is added as a player and a new round recorded.

    A Game is `new` when the first round has not started yet. Once it starts
    it changes to `ongoing`. When a player wins it's changed to `finished`, if
    all players but one quit before the game is over, it's marked as `abandoned`
    """

    name = models.CharField(max_length=64)
    created_on = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _('game')
        verbose_name_plural = _('games')

        ordering = ('-created_on', )


    @property
    def status(self):
        from dixit.game.models.round import RoundStatus

        def all_rounds_complete():
            return all(r.status == RoundStatus.COMPLETE for r in self.rounds.all())

        if self.players.count() == 0:
            return GameStatus.ABANDONED

        elif not self.current_round or all_rounds_complete():
            return GameStatus.FINISHED

        elif self.current_round.number == 0 and self.current_round.status == RoundStatus.NEW:
            return GameStatus.NEW

        return GameStatus.ONGOING

    @property
    def current_round(self):
        rounds = self.rounds.all().order_by('-number')
        if rounds:
            return rounds[0]
        return None

    def __str__(self):
        return self.name

    @property
    def storyteller(self):
        """
        Storyteller of the current round
        """
        return self.current_round.turn

    @classmethod
    def new_game(cls, name, player_name):
        """
        Bootstraps a new game with a round and a storyteller player
        """
        from dixit.game.models import Player, Round

        game = cls(name=name)
        game.save()

        player = Player(game=game, name=player_name, owner=True)
        player.save()

        game.add_round()
        return game

    def add_player(self, player_name):
        """
        Adds a new player to the game and deals cards if a round is available
        """
        from dixit.game.models import Player
        from dixit.game.models.round import RoundStatus

        order = self.players.count()
        player = Player(game=self, name=player_name, order=order)
        player.save()

        if self.current_round and self.current_round.status == RoundStatus.NEW:
            self.current_round.deal()

        return player

    def add_round(self):
        """
        Adds a new round to the game for the next player's turn
        """
        from dixit.game.models import Player, Round

        nplayers = self.players.count()
        if nplayers == 0:
            return None

        if not self.current_round:
            number, turn = 0, 0
        else:
            number = self.current_round.number + 1
            turn = (self.current_round.turn.order + 1) % nplayers

        player = Player.objects.get(game=self, order=turn)
        game_round = Round(game=self, number=number, turn=player)

        try:
            game_round.deal()
            game_round.save()
        except GameDeckExhausted:
            return None

        return game_round

    def complete_round(self):
        """
        Closes the current round and updates the scoring. It also updates the card's
        description based on the performance of the story and the players guesses.

        The scoring works as follows:
            - The storyteller gets GAME_STORY_SCORE points if at least one, but not
              all players vote for the story card
            - The players get GAME_GUESS_SCORE points if they guess the story card
            - The players get GAME_CONFUSED_GUESS_SCORE points for each other player
              that chooses their card
            - The players get GAME_MAX_ROUND_SCORE maximum points
        """
        from dixit.game.models import Play
        from dixit.game.models.round import RoundStatus

        # TODO:
        # Update cards descriptions
        # Storyteller's card gets story added with confidence 50 as a baseline,
        # then gets a bonus based on the ratio of players who correctly guessed
        # the card (eg.: 50 + ((50 / players) * votes))
        # Player card gets story added with confidence based directly on the
        # ratio of guesses (eg: (100 / players) * votes)

        game_round = self.current_round
        storyteller = self.storyteller

        if game_round.status != RoundStatus.COMPLETE:
            raise GameRoundIncomplete('still waiting for players')

        plays = game_round.plays.all()
        players_plays = plays.exclude(player=storyteller)

        story_card = plays.get(player=storyteller).card_provided
        scores = defaultdict(lambda: 0)
        guesses = {p.player: 0 for p in players_plays}

        for play in players_plays:
            if play.card_chosen == story_card:
                scores[play.player] += settings.GAME_GUESS_SCORE
                guesses[play.player] = True
            else:
                chosen_play = plays.get(card_provided=play.card_chosen, game_round=game_round)
                scores[chosen_play.player] += settings.GAME_CONFUSED_GUESS_SCORE

        if any(guesses.values()) and not all(guesses.values()):
            scores[storyteller] = settings.GAME_STORY_SCORE

        for player, score in scores.items():
            player.score += min(settings.GAME_MAX_ROUND_SCORE, score)
            player.save()

        return self