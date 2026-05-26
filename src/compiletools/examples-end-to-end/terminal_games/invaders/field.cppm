// field.cppm -- the pure Space Invaders simulation: interface unit (module invaders.field).
//
// Re-exports invaders.formation and invaders.bullet, declares the Field aggregate
// (which composes a Formation sub-struct and a Bullet sub-struct), and declares
// the simulation entry points. The definitions live in field_impl.cpp. A
// consumer's single `import invaders.field;` brings in the whole invaders graph;
// ct-cake auto-discovers and links formation + bullet from the import edges.
//
// I/O-free and deterministic. Both invaders.cpp and test_field.cpp import this.
//
// CAS: module interface unit -> BMI in cas-pcmdir, object in cas-objdir.
module;

export module invaders.field;

export import invaders.formation;
export import invaders.bullet;

export namespace invaders {

enum class Action  { None, Left, Right, Fire };
enum class Verdict { Playing, Won, Lost };

struct Field {
    int width;
    int height;
    int player_x;
    int ticks;
    Formation formation;
    Bullet    bullet;
};

Field   initial(int width, int height);
Field   step(Field f, Action action);
Verdict classify(const Field& f);

}  // namespace invaders
