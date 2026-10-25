// Text of the task and the games. Included into render.cpp inside the anonymous namespace because it uses its
// primitives (draw_text, fit, wrap2...). The pictures themselves are dots: the ring of a task is in build_led,
// the game scenes are shapes (build_shapes). Positions and sizes follow the web mockup design/screen-rest.html.

const Rgb C_AMBER = {255, 179, 64}, C_OK = {48, 209, 88}, C_FAIL = {255, 69, 58}, C_VIOLET = {191, 90, 242};

// ── Task: what it says under the eyes ──

void draw_task(const Scene &s) {
  const float a = s.task_a;
  if (a < 0.01f || s.task == Task::Idle || !s.task_big || s_y1 < 150 || s_y0 > 205) return;
  const Rgb ink = rgb(s.ink);
  char line[96];
  switch (s.task) {
    case Task::Working: {
      char big[16];
      if (s.task_spin < 0) snprintf(big, sizeof big, "%d%%", (int)(s.task_frac * 100 + 0.5f));
      else snprintf(big, sizeof big, "Работаю");
      draw_text(text::kTitle, big, C, 168, 22.0f / 24, true, ink, a);
      fit(text::kUi, s.task_label, 168, line, sizeof line);
      if (line[0]) draw_text(text::kUi, line, C, 190, 1, true, ink, a * 0.55f);
      break;
    }
    case Task::NeedsInput:
    case Task::NeedsPermission:
      draw_text(text::kUiBold, "Нужен ответ", C, 170, 17.0f / 15, true, C_AMBER, a);
      fit(text::kUi, s.task_label, 168, line, sizeof line);
      if (line[0]) draw_text(text::kUi, line, C, 191, 1, true, ink, a * 0.6f);
      break;
    case Task::Done:
      draw_text(text::kUiBold, "Готово", C, 192, 17.0f / 15, true, C_OK, a);
      break;
    case Task::Failed:
      draw_text(text::kUiBold, "Не получилось", C, 192, 17.0f / 15, true, C_FAIL, a);
      break;
    case Task::Idle:
      break;
  }
}

// ── Games: the result lines ──

const char *const BALL[11] = {"", "Бесспорно", "Да", "Без сомнений", "Точно да", "Конечно",
                              "Спроси позже", "Не ясно", "Не думаю", "Нет", "Вряд ли"};

void draw_game(const Scene &s) {
  const float a = s.game_a, lt = s.game_t;
  if (a < 0.01f || s.game == Game::None || s_y1 < 100 || s_y0 > 226) return;
  const Rgb ink = rgb(s.ink);
  char buf[32];
  switch (s.game) {
    case Game::Coin: {
      const float ra = smooth((lt - 2.4f) / 0.3f) * a;
      if (ra > 0.01f) draw_text(text::kTitle, s.game_p0 ? "Решка" : "Орёл", C, 214, 20.0f / 24, true, ink, ra);
      break;
    }
    case Game::Dice: {
      const float ra = smooth((lt - 1.5f) / 0.3f) * a;
      snprintf(buf, sizeof buf, "Выпало %d", s.game_p0 < 1 ? 1 : (s.game_p0 > 6 ? 6 : s.game_p0));
      if (ra > 0.01f) draw_text(text::kTitle, buf, C, 212, 20.0f / 24, true, ink, ra);
      break;
    }
    case Game::Wheel: {
      const int count = s.game_p0 < 2 ? 2 : (s.game_p0 > 8 ? 8 : s.game_p0);
      const int result = s.game_p1 < 0 ? 0 : (s.game_p1 >= count ? count - 1 : s.game_p1);
      const float ra = smooth((lt - 2.7f) / 0.3f) * a;
      snprintf(buf, sizeof buf, "%d", result + 1);
      if (ra > 0.01f) draw_text(text::kTitle, buf, C, 151, 40.0f / 24, true, ink, ra);
      break;
    }
    case Game::Rps: {
      if (lt < 1.5f) {
        draw_text(text::kUi, "Раз… два… три!", C, 208, 1, true, ink, a * 0.6f);
        break;
      }
      const float ra = smooth((lt - 1.5f) / 0.25f) * a;
      const char *msg = s.game_p1 == 1 ? "Я выиграла" : (s.game_p1 == 2 ? "Ты выиграл" : "Ничья");
      const Rgb c = s.game_p1 == 1 ? C_OK : (s.game_p1 == 2 ? C_AMBER : ink);
      draw_text(text::kTitle, msg, C, 208, 20.0f / 24, true, c, ra);
      break;
    }
    case Game::Magic8: {
      const int ans = s.game_p0 < 1 ? 6 : (s.game_p0 > 10 ? 10 : s.game_p0);
      const float cy = 124, eight = (1 - smooth((lt - 1.2f) / 0.3f)) * a, show = smooth((lt - 1.4f) / 0.4f) * a;
      if (eight > 0.01f) draw_text(text::kTitle, "8", C, cy + 15, 44.0f / 24, true, {235, 240, 255}, eight);
      if (show > 0.01f) {
        char lines[2][128];
        const int n = wrap2(text::kUiBold, BALL[ans], 74, lines);
        const float y0 = n == 1 ? cy + 6 : cy - 2;
        // a single long word ("Бесспорно") must not leave the ball: shrink the whole answer to fit its inner width
        float wmax = 0;
        for (int i = 0; i < n; i++) wmax = fmaxf(wmax, text::width(text::kUiBold, lines[i]));
        const float sc = wmax > 0 ? fminf(17.0f / 15, 86.0f / wmax) : 17.0f / 15;
        for (int i = 0; i < n; i++) draw_text(text::kUiBold, lines[i], C, y0 + i * 20 * sc / (17.0f / 15), sc, true, C_VIOLET, show);
      }
      break;
    }
    case Game::Ttt:
    case Game::None:
      break;
  }
}
