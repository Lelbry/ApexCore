"""Навигационный каркас меню: экраны, стек, обработка глобальных команд.

Базовая абстракция — ``Screen``. Один экземпляр = одно меню (главный
экран, экран расширенного теста CPU, экран настроек и т.п.). Экран
описывает свои пункты и хук обработки выбора. Каркас:

- очищает экран и рисует заголовок (хлебные крошки),
- печатает пункты экрана,
- читает ввод пользователя,
- обрабатывает глобальные команды (back / home / quit / help) до того,
  как передать ввод обработчику экрана.

Глобальные команды
------------------
- ``b`` / ``0`` / ``back`` / ``назад`` — снять верхний экран со стека.
- ``h`` / ``home`` / ``домой`` — вернуться к корневому экрану.
- ``q`` / ``exit`` / ``quit`` / ``выход`` — выйти из приложения.
- ``?`` / ``help`` — показать подсказку по командам.

Управление сигналом
-------------------
``MenuRequest`` — это лёгкий sentinel, чтобы экраны могли выразить
команду каркасу (push/back/home/quit), не работая с самим стеком.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto

from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from apexcore.interfaces.cli.render import console


class NavAction(Enum):
    """Что каркас должен сделать после обработки выбора экраном."""

    STAY = auto()    # остаться на том же экране, перерисовать
    PUSH = auto()    # поставить новый экран поверх
    BACK = auto()    # вернуться на предыдущий экран
    HOME = auto()    # вернуться на корневой экран
    QUIT = auto()    # выйти из приложения


@dataclass
class NavResult:
    """Результат обработки выбора пункта."""

    action: NavAction
    next_screen: Screen | None = None
    flash: str | None = None  # короткое сообщение, показываемое после действия


@dataclass
class MenuItem:
    """Один пункт меню."""

    key: str            # "1", "2", ... — что нажать
    label: str          # человекочитаемая надпись
    handler: Callable[[], NavResult]
    accent: str = "cyan"  # стиль ключа (rich-разметка)


class Screen:
    """Базовый класс экрана меню."""

    title: str = "apexcore"
    subtitle: str | None = None

    def __init__(self) -> None:
        self._items: list[MenuItem] = []
        # Хлебные крошки на момент push'а экрана. Пустой список, пока экран
        # не помещён в MenuLoop. Используется экранами, которые сами
        # перерисовывают шапку (например, накопительная таблица результатов).
        self._breadcrumbs_at_push: list[str] = []

    def items(self) -> list[MenuItem]:
        """Список пунктов меню. Пересчитывается при каждой отрисовке."""
        return self._items

    # Подклассы переопределяют render() если нужны дополнительные блоки
    # (информация о системе, текущее значение настройки и т.п.).
    def render_header(self, breadcrumbs: list[str]) -> None:
        crumbs = "  ›  ".join(breadcrumbs)
        body = f"[bold cyan]{self.title}[/]"
        if self.subtitle:
            body += f"\n[dim]{self.subtitle}[/]"
        body += f"\n[dim]{crumbs}[/]"
        console.print(Panel.fit(body, border_style="cyan"))

    def render_extra(self) -> None:
        """Хук для подклассов: вывод дополнительного контента над пунктами."""
        return None

    def render_items(self) -> None:
        items = self.items()
        if not items:
            console.print("[yellow]Нет пунктов на этом экране[/]")
            return
        tbl = Table(show_header=False, box=None, pad_edge=False, padding=(0, 2))
        tbl.add_column(justify="right")
        tbl.add_column()
        for it in items:
            tbl.add_row(f"[{it.accent}]{it.key}[/]", it.label)
        console.print(tbl)

    def handle_unknown_input(self, choice: str) -> NavResult | None:
        """Опциональный перехват ввода, не совпавшего с item.key.

        Возвращает ``NavResult`` — каркас применит его как обычно
        (PUSH/STAY/BACK/HOME/QUIT). Возвращает ``None`` — каркас покажет
        стандартный flash «Неизвестный пункт».

        Хук вызывается только после того, как ввод не попал ни в HELP/QUIT/
        BACK/HOME-наборы, ни в один из пунктов экрана. Используется экранами,
        которые принимают свободный текстовый ввод (например, экран выбора
        номеров тестов: «1,2,3»).

        Базовая реализация возвращает ``None`` — никакие существующие экраны
        этим не задеваются.
        """
        return None


# ─── Хелперы для построения экранов ────────────────────────────────────────


def push(screen: Screen, flash: str | None = None) -> NavResult:
    """Шорткат: «открыть подменю»."""
    return NavResult(action=NavAction.PUSH, next_screen=screen, flash=flash)


def stay(flash: str | None = None) -> NavResult:
    """Шорткат: «остаться на текущем экране»."""
    return NavResult(action=NavAction.STAY, flash=flash)


def back(flash: str | None = None) -> NavResult:
    """Шорткат: «вернуться на предыдущий экран»."""
    return NavResult(action=NavAction.BACK, flash=flash)


def home(flash: str | None = None) -> NavResult:
    """Шорткат: «вернуться на главный экран»."""
    return NavResult(action=NavAction.HOME, flash=flash)


def quit_app(flash: str | None = None) -> NavResult:
    """Шорткат: «выйти из приложения»."""
    return NavResult(action=NavAction.QUIT, flash=flash)


# ─── Глобальные ключи навигации ────────────────────────────────────────────
#
# Кросс-раскладка (RU/EN): для каждого однобуквенного шортката добавляем
# букву, которая лежит на той же физической клавише в русской раскладке.
# Чтобы пользователь мог набрать «назад» как ``b`` (EN), так и ``и`` (RU) —
# та же клавиша, разная раскладка.
#   B = И,   H = Р,   Q = Й
# Дополнительно принимаем полные русские слова (назад, домой, главная, выход,
# помощь) — это уже было в исходной версии.

BACK_KEYS = frozenset({"b", "и", "0", "back", "назад", "<", "..", "..."})
HOME_KEYS = frozenset({"h", "р", "home", "домой", "main", "главная"})
QUIT_KEYS = frozenset({"q", "й", "quit", "exit", "выход"})
HELP_KEYS = frozenset({"?", "help", "помощь"})

# Подтверждения y/n. По физической клавише Y лежит русская «н», что
# семантически означает «нет» — поэтому raw layout-mapping здесь применять
# нельзя, иначе RU-пользователь, нажав ту же клавишу что и «y», получит
# обратное действие. Используем только семантически корректные русские
# эквиваленты: «д»/«да» — это yes, «н»/«нет» — это no. Они и так лежат на
# отдельных клавишах (Y → н, D → д), так что путаницы не возникает.
CONFIRM_YES_KEYS = frozenset({"y", "yes", "д", "да"})
CONFIRM_NO_KEYS = frozenset({"n", "no", "н", "нет"})


def _print_help() -> None:
    tbl = Table(title="Подсказки навигации", show_header=False, box=None)
    tbl.add_column("Ввод", style="bold cyan")
    tbl.add_column("Что делает")
    tbl.add_row("1, 2, 3, …", "выбрать пункт меню по номеру")
    tbl.add_row("b / и / 0 / назад", "вернуться на предыдущий экран")
    tbl.add_row("h / р / home / главная", "вернуться на главный экран")
    tbl.add_row("q / й / exit / выход", "выйти из приложения")
    tbl.add_row("? / help / помощь", "показать эту подсказку")
    tbl.add_row("Ctrl+C", "во время теста — отмена; в меню — то же, что 'назад'")
    tbl.add_row(
        "[dim](RU)[/]",
        "однобуквенные шорткаты работают с русской раскладки на тех же клавишах",
    )
    console.print(tbl)


# ─── Цикл навигации ────────────────────────────────────────────────────────


class MenuLoop:
    """Стек экранов и главный цикл."""

    def __init__(self, root: Screen) -> None:
        self._stack: list[Screen] = [root]
        self._flash: str | None = None

    @property
    def current(self) -> Screen:
        return self._stack[-1]

    @property
    def breadcrumbs(self) -> list[str]:
        return [s.title for s in self._stack]

    def push(self, screen: Screen) -> None:
        screen._breadcrumbs_at_push = [s.title for s in self._stack] + [screen.title]
        self._stack.append(screen)

    def back(self) -> None:
        if len(self._stack) > 1:
            self._stack.pop()

    def home(self) -> None:
        del self._stack[1:]

    def render(self) -> None:
        console.clear()
        self.current.render_header(self.breadcrumbs)
        self.current.render_extra()
        console.print()
        self.current.render_items()
        console.print()
        if self._flash:
            console.print(self._flash)
            console.print()
            self._flash = None

    def prompt_choice(self) -> str | None:
        """Прочитать ввод. Возвращает None при EOF (Ctrl+D / закрыт stdin)."""
        # Не передаём choices в Prompt.ask — иначе rich будет валидировать строго,
        # а у нас глобальные ключи навигации (b/h/q/?) поверх локальных номеров.
        try:
            raw = Prompt.ask(
                "[bold]Выбор[/] [dim](? — помощь, b — назад, h — главная, q — выход)[/]",
                default="",
            )
        except EOFError:
            return None
        return raw.strip().lower()

    def run(self) -> None:
        """Главный цикл. Возврат — естественный выход (q)."""
        while True:
            self.render()
            choice = self.prompt_choice()
            if choice is None:
                return  # EOF
            # Пустой ввод (Enter без символов) — пользователь «передумал»
            # или хотел просто перерисовать экран. Не показываем «Неизвестный
            # пункт» — это создавало мусорное предупреждение даже при
            # случайном двойном Enter. closes #13.
            if choice == "":
                continue
            # Глобальные ключи имеют приоритет над пунктами экрана.
            if choice in HELP_KEYS:
                _print_help()
                _wait_enter()
                continue
            if choice in QUIT_KEYS:
                return
            if choice in BACK_KEYS:
                if len(self._stack) > 1:
                    self.back()
                else:
                    # На корневом «назад» = выход с подтверждением: чтобы
                    # пользователь не закрывал приложение случайно по 0.
                    if _confirm("Выйти из apexcore? (y/n)"):
                        return
                continue
            if choice in HOME_KEYS:
                self.home()
                continue

            # Локальный пункт.
            item = next((i for i in self.current.items() if i.key == choice), None)
            if item is None:
                # Дать экрану шанс перехватить нестандартный ввод
                # (например, «1,2,3» на экране выбора тестов).
                try:
                    custom = self.current.handle_unknown_input(choice)
                except KeyboardInterrupt:
                    self._flash = "[yellow]Отменено пользователем[/]"
                    continue
                except Exception as exc:
                    self._flash = f"[red]Ошибка: {exc}[/]"
                    continue
                if custom is not None:
                    try:
                        self._apply(custom)
                    except _ExitMenuError:
                        return
                    continue
                self._flash = f"[yellow]Неизвестный пункт: {choice!r}. ? — помощь.[/]"
                continue
            try:
                result = item.handler()
            except KeyboardInterrupt:
                # Если пункт сам не перехватил Ctrl+C — это значит, что
                # пользователь нажал отмену вне активного теста. Просто
                # остаёмся на текущем экране.
                self._flash = "[yellow]Отменено пользователем[/]"
                continue
            except Exception as exc:
                self._flash = f"[red]Ошибка: {exc}[/]"
                continue
            try:
                self._apply(result)
            except _ExitMenuError:
                return

    def _apply(self, result: NavResult) -> None:
        if result.flash:
            self._flash = result.flash
        if result.action == NavAction.STAY:
            return
        if result.action == NavAction.PUSH and result.next_screen is not None:
            self.push(result.next_screen)
            return
        if result.action == NavAction.BACK:
            self.back()
            return
        if result.action == NavAction.HOME:
            self.home()
            return
        if result.action == NavAction.QUIT:
            # Сигнал на выход — завершаем цикл наружу через stop-флаг.
            self._stack.clear()
            raise _ExitMenuError()


class _ExitMenuError(Exception):
    """Внутренний сигнал «выйти из меню по запросу экрана»."""


def _wait_enter(message: str = "[dim]Enter — продолжить[/]") -> None:
    with contextlib.suppress(EOFError):
        Prompt.ask(message, default="")


def _confirm(message: str) -> bool:
    """y/n-подтверждение. Принимает y/yes/д/да как «да», остальное — «нет»."""
    try:
        ans = Prompt.ask(message, default="n").strip().lower()
    except EOFError:
        return False
    return ans in CONFIRM_YES_KEYS


__all__ = [
    "BACK_KEYS",
    "CONFIRM_NO_KEYS",
    "CONFIRM_YES_KEYS",
    "HELP_KEYS",
    "HOME_KEYS",
    "QUIT_KEYS",
    "MenuItem",
    "MenuLoop",
    "NavAction",
    "NavResult",
    "Screen",
    "_ExitMenuError",
    "_confirm",
    "_wait_enter",
    "back",
    "home",
    "push",
    "quit_app",
    "stay",
]
