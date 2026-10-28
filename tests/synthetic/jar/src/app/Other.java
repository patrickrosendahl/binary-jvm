package app;

// extends a class of the nested lib/helper.jar (jvm-120: @Override in the class view)
public class Other extends lib.Base {
    int twice(int x) {
        return x * 2;
    }

    @Override
    public String name() {
        return lib.Helper.join("other", "x");
    }
}
